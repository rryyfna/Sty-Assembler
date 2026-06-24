import os
import mido
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk

# ==========================================
# 1. LOGIKA INTI MIDI, CASM, & SCALING
# ==========================================

def extract_casm_full(filepath):
    try:
        with open(filepath, 'rb') as f:
            data = f.read()
        idx = data.find(b'CASM')
        if idx == -1: return b"", b""
        
        casm_len = int.from_bytes(data[idx+4:idx+8], 'big')
        casm_block = data[idx:idx+8+casm_len]
        trailer = data[idx+8+casm_len:]
        return casm_block, trailer
    except Exception:
        return b"", b""

def parse_casm(casm_block):
    if not casm_block.startswith(b'CASM'): return []
    casm_data = casm_block[8:]
    csegs = []
    idx = 0
    while idx < len(casm_data):
        chunk_type = casm_data[idx:idx+4]
        chunk_len = int.from_bytes(casm_data[idx+4:idx+8], 'big')
        chunk_full = casm_data[idx:idx+8+chunk_len]
        if chunk_type == b'CSEG':
            sdec_idx = chunk_full.find(b'Sdec')
            name = ""
            if sdec_idx != -1:
                name_len = int.from_bytes(chunk_full[sdec_idx+4:sdec_idx+8], 'big')
                name = chunk_full[sdec_idx+8:sdec_idx+8+name_len].decode('ascii', errors='ignore').strip('\x00')
            csegs.append({'name': name, 'data': chunk_full, 'type': chunk_type})
        else:
            csegs.append({'name': None, 'data': chunk_full, 'type': chunk_type})
        idx += 8 + chunk_len
    return csegs

def rename_cseg(cseg_dict, new_name):
    data = cseg_dict['data']
    sdec_idx = data.find(b'Sdec')
    if sdec_idx != -1:
        old_name_len = int.from_bytes(data[sdec_idx+4:sdec_idx+8], 'big')
        new_name_bytes = new_name.encode('ascii')
        new_sdec_len = len(new_name_bytes)
        new_sdec = b'Sdec' + new_sdec_len.to_bytes(4, 'big') + new_name_bytes
        
        length_diff = new_sdec_len - old_name_len
        cseg_len = int.from_bytes(data[4:8], 'big')
        new_cseg_len = cseg_len + length_diff
        
        new_data = b'CSEG' + new_cseg_len.to_bytes(4, 'big') + data[8:sdec_idx] + new_sdec + data[sdec_idx+8+old_name_len:]
        return {'name': new_name, 'data': new_data, 'type': b'CSEG'}
    return cseg_dict

def build_casm(csegs):
    body = b"".join(c['data'] for c in csegs)
    return b'CASM' + len(body).to_bytes(4, 'big') + body

def track_to_absolute(track):
    abs_track = []
    current_time = 0
    for msg in track:
        current_time += msg.time
        abs_track.append({'time': current_time, 'msg': msg})
    return abs_track

def absolute_to_track(abs_track):
    abs_track.sort(key=lambda x: x['time']) 
    new_track = mido.MidiTrack()
    current_time = 0
    for item in abs_track:
        msg = item['msg'].copy()
        msg.time = item['time'] - current_time
        new_track.append(msg)
        current_time = item['time']
    return new_track

def scale_absolute_track(abs_track, ratio):
    scaled_track = []
    for item in abs_track:
        new_time = int(round(item['time'] * ratio))
        scaled_track.append({'time': new_time, 'msg': item['msg']})
    return scaled_track

def get_marker_bounds(abs_track, marker_name):
    start_time = -1
    end_time = -1
    in_section = False
    
    for item in abs_track:
        if item['msg'].type == 'marker':
            if item['msg'].text == marker_name:
                in_section = True
                start_time = item['time']
                continue
            elif in_section:
                end_time = item['time']
                break
                
    if in_section and end_time == -1:
        end_time = abs_track[-1]['time']
        
    return start_time, end_time

def extract_section(abs_track, start_time, end_time):
    return [item for item in abs_track if start_time <= item['time'] < end_time]

# ==========================================
# 2. LOGIKA PENGGABUNGAN & INJEKSI RESET
# ==========================================

def process_assembly(template_path, custom_path, output_path, markers_to_replace, ratio_mode, inject_reset, reset_hb):
    try:
        temp_casm_block, temp_trailer = extract_casm_full(template_path)
        cust_casm_block, _ = extract_casm_full(custom_path)
        
        if not temp_casm_block:
            return False, "File template tidak memiliki data CASM yang valid!"
            
        temp_csegs = parse_casm(temp_casm_block) if temp_casm_block else []
        cust_csegs = parse_casm(cust_casm_block) if cust_casm_block else []

        mid_temp = mido.MidiFile(template_path)
        mid_cust = mido.MidiFile(custom_path)
        
        temp_ppq = mid_temp.ticks_per_beat
        cust_ppq = mid_cust.ticks_per_beat
        
        if ratio_mode.startswith("Auto"):
            ppq_ratio = temp_ppq / cust_ppq
        else:
            try:
                ppq_ratio = float(ratio_mode.split()[0])
            except ValueError:
                ppq_ratio = 1.0 
        
        temp_abs = track_to_absolute(mid_temp.tracks[0])
        cust_abs = track_to_absolute(mid_cust.tracks[0])
        
        if ppq_ratio != 1.0:
            cust_abs = scale_absolute_track(cust_abs, ppq_ratio)
            
        # --- RESET HARMONIC (71) & BRIGHTNESS (74) KHUSUS BASS (CH 11) ---
        if reset_hb:
            for item in temp_abs:
                msg = item['msg']
                if msg.type == 'control_change' and msg.channel == 10:
                    if msg.control in (71, 74):
                        new_msg = msg.copy()
                        new_msg.value = 64
                        item['msg'] = new_msg
        # -----------------------------------------------------------------
        
        final_abs = temp_abs.copy()
        
        for marker in markers_to_replace:
            if ">" in marker:
                src_marker, dst_marker = [m.strip() for m in marker.split(">")]
            else:
                src_marker = dst_marker = marker.strip()
                
            cust_start, cust_end = get_marker_bounds(cust_abs, src_marker)
            if cust_start == -1:
                print(f"Info: {src_marker} tidak ditemukan di lagu baru.")
                continue
                
            cust_section = extract_section(cust_abs, cust_start, cust_end)
            cust_length = cust_end - cust_start
            
            temp_start, temp_end = get_marker_bounds(final_abs, dst_marker)
            if temp_start == -1:
                print(f"Info: {dst_marker} tidak ditemukan di template.")
                continue
                
            temp_length = temp_end - temp_start
            shift_amount = cust_length - temp_length
            
            new_final_abs = []
            
            for item in final_abs:
                if item['time'] < temp_start:
                    new_final_abs.append(item)
                elif item['time'] >= temp_end:
                    new_final_abs.append({'time': item['time'] + shift_amount, 'msg': item['msg']})
            
            if inject_reset:
                for ch in range(16):
                    new_final_abs.append({
                        'time': temp_start, 
                        'msg': mido.Message('pitchwheel', channel=ch, pitch=0)
                    })
                    new_final_abs.append({
                        'time': temp_start, 
                        'msg': mido.Message('control_change', channel=ch, control=1, value=0)
                    })
            
            for item in cust_section:
                normalized_time = temp_start + (item['time'] - cust_start)
                new_final_abs.append({'time': normalized_time, 'msg': item['msg']})
                
            final_abs = new_final_abs
            
            # --- REPLACE CASM CSEG ---
            src_cseg = next((c for c in cust_csegs if c.get('name') == src_marker), None)
            if src_cseg:
                temp_csegs = [c for c in temp_csegs if c.get('name') != dst_marker]
                new_cseg = rename_cseg(src_cseg, dst_marker)
                temp_csegs.append(new_cseg)

        mid_temp.tracks[0] = absolute_to_track(final_abs)
        temp_output = "temp_output.mid"
        mid_temp.save(temp_output)
        
        with open(temp_output, 'rb') as f:
            midi_bytes = f.read()
            
        final_casm_data = build_casm(temp_csegs) + temp_trailer if temp_csegs else temp_casm_block + temp_trailer
            
        with open(output_path, 'wb') as f:
            f.write(midi_bytes + final_casm_data)
            
        os.remove(temp_output)
        return True, "Assembly Berhasil! Bass Ch 11 disesuaikan."
        
    except Exception as e:
        return False, str(e)

# ==========================================
# 3. ANTARMUKA PENGGUNA (UI CTK DARK MODE)
# ==========================================

class StyleAssemblerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("rryystudios @rryynaufal")
        self.root.geometry("1000x700")
        
        # Pengaturan Tema CTK
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")
        
        self.BG_MAIN = "#121212"
        self.BG_PANEL = "#1E1E1E"
        self.BG_MENU = "#080808"
        self.FG_GREEN = "#39FF14"
        self.ACTIVE_GREEN = "#1FA31F" 
        self.BTN_RADIUS = 8
        self.INPUT_HEIGHT = 35 # Tinggi elemen input (Entry, Button Browse, Combobox) disamakan
        
        # Variabel UI
        self.template_var = tk.StringVar()
        self.custom_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.ratio_var = tk.StringVar(value="Auto (Hitung Otomatis)")
        self.marker_var = tk.StringVar(value="Ending A, Ending B")
        self.inject_reset_var = tk.BooleanVar(value=True)
        self.reset_hb_var = tk.BooleanVar(value=True) 
        
        self.create_widgets()

    def create_widgets(self):
        # --- TOP HEADER BAR ---
        top_frame = ctk.CTkFrame(self.root, fg_color=self.BG_PANEL, height=60, corner_radius=0)
        top_frame.pack(fill="x", side="top")
        top_frame.pack_propagate(False)
        
        ctk.CTkLabel(top_frame, text="rryystudios", font=ctk.CTkFont(size=18, weight="bold"), text_color="white").pack(side="left", padx=20)

        btn_save = ctk.CTkButton(top_frame, text="ASSEMBLY", fg_color="#228B22", hover_color="#32CD32", 
                                 font=ctk.CTkFont(weight="bold"), corner_radius=self.BTN_RADIUS, height=35, command=self.run_assembly)
        btn_save.pack(side="right", padx=22, pady=12)

        # --- BODY CONTAINERS ---
        body_frame = ctk.CTkFrame(self.root, fg_color=self.BG_MAIN, corner_radius=0)
        body_frame.pack(fill="both", expand=True)

        # --- LEFT MENU (Dummy Visual ala Yamaha PSR SX) ---
        left_menu = ctk.CTkFrame(body_frame, fg_color=self.BG_MENU, width=180, corner_radius=0)
        left_menu.pack(side="left", fill="y")
        left_menu.pack_propagate(False)
        
        menus = ["    Basic", "    Rec Channel", "    Assembly", "    Channel Edit", "    SFF Edit"]
        for m in menus:
            is_active = (m == "    Assembly")
            bg_color = self.ACTIVE_GREEN if is_active else self.BG_MENU
            text_color = "white" if is_active else "#666666"
            hover_bg = self.ACTIVE_GREEN if is_active else "#1A1A1A"
            
            btn = ctk.CTkButton(left_menu, text=f"   {m}", fg_color=bg_color, text_color=text_color, hover_color=hover_bg,
                                font=ctk.CTkFont(size=16, weight="bold"), corner_radius=0, anchor="w", height=55)
            btn.pack(fill="x")
            
            if not is_active:
                ctk.CTkFrame(left_menu, fg_color="#1A1A1A", height=1).pack(fill="x")

        # --- RIGHT MAIN AREA ---
        right_area = ctk.CTkFrame(body_frame, fg_color=self.BG_MAIN, corner_radius=0)
        right_area.pack(side="left", fill="both", expand=True, padx=30, pady=20)

        # --- SECTION: FILE MANAGEMENT ---
        ctk.CTkLabel(right_area, text="File Management", font=ctk.CTkFont(size=18, weight="bold"), text_color="white").pack(anchor="w", pady=(0, 15))
        
        file_frame = ctk.CTkFrame(right_area, fg_color=self.BG_PANEL, corner_radius=self.BTN_RADIUS)
        file_frame.pack(fill="x", pady=(0, 20))
        
        # Grid konfigurasi untuk form
        file_frame.grid_columnconfigure(1, weight=1)

        # 1. Template
        ctk.CTkLabel(file_frame, text="Template (.sty):", font=ctk.CTkFont(size=14)).grid(row=0, column=0, padx=15, pady=(15, 10), sticky="w")
        ctk.CTkEntry(file_frame, textvariable=self.template_var, state="readonly", height=self.INPUT_HEIGHT, fg_color="#181818", border_color="#333").grid(row=0, column=1, padx=10, pady=(15, 10), sticky="we")
        ctk.CTkButton(file_frame, text="Browse", width=80, height=self.INPUT_HEIGHT, fg_color="#333", hover_color="#555", corner_radius=self.BTN_RADIUS, command=self.browse_template).grid(row=0, column=2, padx=15, pady=(15, 10))

        # 2. Custom MIDI
        ctk.CTkLabel(file_frame, text="MIDI Baru (.mid):", font=ctk.CTkFont(size=14)).grid(row=1, column=0, padx=15, pady=10, sticky="w")
        ctk.CTkEntry(file_frame, textvariable=self.custom_var, state="readonly", height=self.INPUT_HEIGHT, fg_color="#181818", border_color="#333").grid(row=1, column=1, padx=10, pady=10, sticky="we")
        ctk.CTkButton(file_frame, text="Browse", width=80, height=self.INPUT_HEIGHT, fg_color="#333", hover_color="#555", corner_radius=self.BTN_RADIUS, command=self.browse_custom).grid(row=1, column=2, padx=15, pady=10)

        # 3. Output
        ctk.CTkLabel(file_frame, text="Simpan (.sty):", font=ctk.CTkFont(size=14)).grid(row=2, column=0, padx=15, pady=(10, 15), sticky="w")
        ctk.CTkEntry(file_frame, textvariable=self.output_var, state="readonly", height=self.INPUT_HEIGHT, fg_color="#181818", border_color="#333").grid(row=2, column=1, padx=10, pady=(10, 15), sticky="we")
        ctk.CTkButton(file_frame, text="Browse", width=80, height=self.INPUT_HEIGHT, fg_color="#333", hover_color="#555", corner_radius=self.BTN_RADIUS, command=self.browse_output).grid(row=2, column=2, padx=15, pady=(10, 15))


        # --- SECTION: KONFIGURASI & FILTER ---
        ctk.CTkLabel(right_area, text="Konfigurasi & Filter MIDI", font=ctk.CTkFont(size=18, weight="bold"), text_color="white").pack(anchor="w", pady=(10, 15))
        
        config_frame = ctk.CTkFrame(right_area, fg_color=self.BG_PANEL, corner_radius=self.BTN_RADIUS)
        config_frame.pack(fill="x")
        config_frame.grid_columnconfigure(1, weight=1)

        # Target Marker
        ctk.CTkLabel(config_frame, text="Target Marker:", font=ctk.CTkFont(size=14)).grid(row=0, column=0, padx=15, pady=(15, 10), sticky="w")
        ctk.CTkEntry(config_frame, textvariable=self.marker_var, height=self.INPUT_HEIGHT, fg_color="#181818", border_color="#333").grid(row=0, column=1, padx=10, pady=(15, 10), sticky="we")

        # Rasio Resolusi
        ctk.CTkLabel(config_frame, text="Rasio Resolusi:", font=ctk.CTkFont(size=14)).grid(row=1, column=0, padx=15, pady=10, sticky="w")
        combo_ratio = ctk.CTkComboBox(config_frame, variable=self.ratio_var, state="readonly", height=self.INPUT_HEIGHT,
                                      values=["Auto (Hitung Otomatis)", "1.0 (Tanpa Skala / Asli)", "2.0 (Kali 2)", "0.5 (Bagi 2)"],
                                      fg_color="#181818", border_color="#333", button_color="#333", button_hover_color="#555")
        combo_ratio.grid(row=1, column=1, padx=10, pady=10, sticky="we")

        # Checkboxes
        ctk.CTkCheckBox(config_frame, text="Reset Pitchbend & Modulasi (0)", variable=self.inject_reset_var, fg_color=self.ACTIVE_GREEN, hover_color=self.FG_GREEN).grid(row=2, column=0, columnspan=2, padx=15, pady=(10, 5), sticky="w")
        ctk.CTkCheckBox(config_frame, text="Harmonic & Brightness Bass 64", variable=self.reset_hb_var, fg_color=self.ACTIVE_GREEN, hover_color=self.FG_GREEN).grid(row=3, column=0, columnspan=2, padx=15, pady=(5, 15), sticky="w")

        # --- STATUS LABEL ---
        self.status_label = ctk.CTkLabel(right_area, text="Status: Siap memproses...", font=ctk.CTkFont(size=14, slant="italic"), text_color="#AAAAAA")
        self.status_label.pack(anchor="w", pady=20)


    def browse_template(self):
        filename = filedialog.askopenfilename(filetypes=[("Yamaha Style", "*.sty")])
        if filename: self.template_var.set(filename)

    def browse_custom(self):
        filename = filedialog.askopenfilename(filetypes=[("MIDI Files", "*.mid"), ("Yamaha Style", "*.sty")])
        if filename: self.custom_var.set(filename)

    def browse_output(self):
        filename = filedialog.asksaveasfilename(defaultextension=".sty", filetypes=[("Yamaha Style", "*.sty")])
        if filename: self.output_var.set(filename)

    def run_assembly(self):
        template = self.template_var.get()
        custom = self.custom_var.get()
        output = self.output_var.get()
        markers = [m.strip() for m in self.marker_var.get().split(",")]
        ratio_mode = self.ratio_var.get()
        inject_reset = self.inject_reset_var.get()
        reset_hb = self.reset_hb_var.get() 

        if not template or not custom or not output:
            messagebox.showerror("Error", "Harap isi semua kolom file!")
            return

        self.status_label.configure(text="Status: Memproses assembly dan filter...", text_color="#f39c12")
        self.root.update()

        success, msg = process_assembly(template, custom, output, markers, ratio_mode, inject_reset, reset_hb)
        
        if success:
            self.status_label.configure(text=f"Status: {msg}", text_color=self.FG_GREEN)
            messagebox.showinfo("Berhasil", f"File berhasil disimpan di:\n{output}")
        else:
            self.status_label.configure(text="Status: Gagal memproses file", text_color="#c0392b")
            messagebox.showerror("Gagal", f"Terjadi Error:\n{msg}")

if __name__ == "__main__":
    root = ctk.CTk()
    app = StyleAssemblerApp(root)
    root.mainloop()
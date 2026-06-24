import os
import mido
import sys
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QLineEdit, QComboBox,
                             QCheckBox, QFrame, QFileDialog, QMessageBox, QGridLayout)
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtCore import Qt

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

class StyleAssemblerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("rryystudios @rryynaufal")
        self.resize(1000, 700)
        
        self.BG_MAIN = "#121212"
        self.BG_PANEL = "#1E1E1E"
        self.BG_MENU = "#080808"
        self.FG_GREEN = "#39FF14"
        self.ACTIVE_GREEN = "#1FA31F" 
        self.INPUT_HEIGHT = 35
        
        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {self.BG_MAIN}; }}
            QLabel {{ color: white; }}
            QLineEdit {{ 
                background-color: #181818; 
                border: 1px solid #333; 
                color: white; 
                padding: 5px;
                border-radius: 4px;
            }}
            QComboBox {{
                background-color: #181818;
                border: 1px solid #333;
                color: white;
                padding: 5px;
                border-radius: 4px;
            }}
            QComboBox::drop-down {{ border: 0px; }}
            QCheckBox {{ color: white; }}
            QCheckBox::indicator:checked {{
                background-color: {self.ACTIVE_GREEN};
                border: 1px solid {self.ACTIVE_GREEN};
            }}
            QPushButton {{
                color: white;
                border-radius: 4px;
                padding: 5px;
            }}
        """)
        
        self.create_widgets()

    def create_widgets(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # --- TOP HEADER BAR ---
        top_frame = QFrame()
        top_frame.setFixedHeight(60)
        top_frame.setStyleSheet(f"background-color: {self.BG_PANEL};")
        top_layout = QHBoxLayout(top_frame)
        top_layout.setContentsMargins(20, 0, 20, 0)
        
        title_label = QLabel("rryystudios")
        title_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        top_layout.addWidget(title_label)
        
        top_layout.addStretch()
        
        btn_save = QPushButton("ASSEMBLY")
        btn_save.setFixedSize(120, 35)
        btn_save.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        btn_save.setStyleSheet(f"""
            QPushButton {{ background-color: #228B22; border-radius: 8px; }}
            QPushButton:hover {{ background-color: #32CD32; }}
        """)
        btn_save.clicked.connect(self.run_assembly)
        top_layout.addWidget(btn_save)
        
        main_layout.addWidget(top_frame)

        # --- BODY CONTAINERS ---
        body_frame = QFrame()
        body_layout = QHBoxLayout(body_frame)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        
        # --- LEFT MENU ---
        left_menu = QFrame()
        left_menu.setFixedWidth(180)
        left_menu.setStyleSheet(f"background-color: {self.BG_MENU};")
        left_layout = QVBoxLayout(left_menu)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(1)
        
        menus = ["Basic", "Rec Channel", "Assembly", "Channel Edit", "SFF Edit"]
        for m in menus:
            is_active = (m == "Assembly")
            bg_color = self.ACTIVE_GREEN if is_active else self.BG_MENU
            text_color = "white" if is_active else "#666666"
            hover_bg = self.ACTIVE_GREEN if is_active else "#1A1A1A"
            
            btn = QPushButton(f"    {m}")
            btn.setFixedHeight(55)
            btn.setFont(QFont("Arial", 11, QFont.Weight.Bold))
            btn.setStyleSheet(f"""
                QPushButton {{ 
                    background-color: {bg_color}; 
                    color: {text_color}; 
                    text-align: left;
                    border: none;
                }}
                QPushButton:hover {{ background-color: {hover_bg}; }}
            """)
            left_layout.addWidget(btn)
        
        left_layout.addStretch()
        body_layout.addWidget(left_menu)

        # --- RIGHT MAIN AREA ---
        right_area = QFrame()
        right_area.setStyleSheet(f"background-color: {self.BG_MAIN};")
        right_layout = QVBoxLayout(right_area)
        right_layout.setContentsMargins(30, 20, 30, 20)
        right_layout.setSpacing(20)
        
        # --- SECTION: FILE MANAGEMENT ---
        lbl_file = QLabel("File Management")
        lbl_file.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        right_layout.addWidget(lbl_file)
        
        file_frame = QFrame()
        file_frame.setStyleSheet(f"background-color: {self.BG_PANEL}; border-radius: 8px;")
        file_layout = QGridLayout(file_frame)
        file_layout.setContentsMargins(15, 15, 15, 15)
        file_layout.setSpacing(10)
        
        # Template
        lbl_temp = QLabel("Template (.sty):")
        lbl_temp.setFont(QFont("Arial", 11))
        self.template_input = QLineEdit()
        self.template_input.setFixedHeight(self.INPUT_HEIGHT)
        self.template_input.setReadOnly(True)
        btn_temp = QPushButton("Browse")
        btn_temp.setFixedSize(80, self.INPUT_HEIGHT)
        btn_temp.setStyleSheet("QPushButton { background-color: #333; } QPushButton:hover { background-color: #555; }")
        btn_temp.clicked.connect(self.browse_template)
        
        file_layout.addWidget(lbl_temp, 0, 0)
        file_layout.addWidget(self.template_input, 0, 1)
        file_layout.addWidget(btn_temp, 0, 2)
        
        # Custom MIDI
        lbl_cust = QLabel("MIDI Baru (.mid):")
        lbl_cust.setFont(QFont("Arial", 11))
        self.custom_input = QLineEdit()
        self.custom_input.setFixedHeight(self.INPUT_HEIGHT)
        self.custom_input.setReadOnly(True)
        btn_cust = QPushButton("Browse")
        btn_cust.setFixedSize(80, self.INPUT_HEIGHT)
        btn_cust.setStyleSheet("QPushButton { background-color: #333; } QPushButton:hover { background-color: #555; }")
        btn_cust.clicked.connect(self.browse_custom)
        
        file_layout.addWidget(lbl_cust, 1, 0)
        file_layout.addWidget(self.custom_input, 1, 1)
        file_layout.addWidget(btn_cust, 1, 2)
        
        # Output
        lbl_out = QLabel("Simpan (.sty):")
        lbl_out.setFont(QFont("Arial", 11))
        self.output_input = QLineEdit()
        self.output_input.setFixedHeight(self.INPUT_HEIGHT)
        self.output_input.setReadOnly(True)
        btn_out = QPushButton("Browse")
        btn_out.setFixedSize(80, self.INPUT_HEIGHT)
        btn_out.setStyleSheet("QPushButton { background-color: #333; } QPushButton:hover { background-color: #555; }")
        btn_out.clicked.connect(self.browse_output)
        
        file_layout.addWidget(lbl_out, 2, 0)
        file_layout.addWidget(self.output_input, 2, 1)
        file_layout.addWidget(btn_out, 2, 2)
        
        file_layout.setColumnStretch(1, 1)
        right_layout.addWidget(file_frame)

        # --- SECTION: KONFIGURASI & FILTER ---
        lbl_conf = QLabel("Konfigurasi & Filter MIDI")
        lbl_conf.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        right_layout.addWidget(lbl_conf)
        
        config_frame = QFrame()
        config_frame.setStyleSheet(f"background-color: {self.BG_PANEL}; border-radius: 8px;")
        config_layout = QGridLayout(config_frame)
        config_layout.setContentsMargins(15, 15, 15, 15)
        config_layout.setSpacing(10)
        
        # Target Marker
        lbl_marker = QLabel("Target Marker:")
        lbl_marker.setFont(QFont("Arial", 11))
        self.marker_input = QLineEdit("Ending A, Ending B")
        self.marker_input.setFixedHeight(self.INPUT_HEIGHT)
        config_layout.addWidget(lbl_marker, 0, 0)
        config_layout.addWidget(self.marker_input, 0, 1)
        
        # Rasio Resolusi
        lbl_ratio = QLabel("Rasio Resolusi:")
        lbl_ratio.setFont(QFont("Arial", 11))
        self.ratio_combo = QComboBox()
        self.ratio_combo.setFixedHeight(self.INPUT_HEIGHT)
        self.ratio_combo.addItems(["Auto (Hitung Otomatis)", "1.0 (Tanpa Skala / Asli)", "2.0 (Kali 2)", "0.5 (Bagi 2)"])
        config_layout.addWidget(lbl_ratio, 1, 0)
        config_layout.addWidget(self.ratio_combo, 1, 1)
        
        # Checkboxes
        self.inject_reset_cb = QCheckBox("Reset Pitchbend & Modulasi (0)")
        self.inject_reset_cb.setFont(QFont("Arial", 10))
        self.inject_reset_cb.setChecked(True)
        config_layout.addWidget(self.inject_reset_cb, 2, 0, 1, 2)
        
        self.reset_hb_cb = QCheckBox("Harmonic & Brightness Bass 64")
        self.reset_hb_cb.setFont(QFont("Arial", 10))
        self.reset_hb_cb.setChecked(True)
        config_layout.addWidget(self.reset_hb_cb, 3, 0, 1, 2)
        
        config_layout.setColumnStretch(1, 1)
        right_layout.addWidget(config_frame)
        
        # --- STATUS LABEL ---
        right_layout.addStretch()
        self.status_label = QLabel("Status: Siap memproses...")
        font = QFont("Arial", 11)
        font.setItalic(True)
        self.status_label.setFont(font)
        self.status_label.setStyleSheet("color: #AAAAAA;")
        right_layout.addWidget(self.status_label)

        body_layout.addWidget(right_area)
        main_layout.addWidget(body_frame)

    def browse_template(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Pilih Template", "", "Yamaha Style (*.sty)")
        if filename: self.template_input.setText(filename)

    def browse_custom(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Pilih MIDI Baru", "", "MIDI/Style (*.mid *.sty)")
        if filename: self.custom_input.setText(filename)

    def browse_output(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Simpan File", "", "Yamaha Style (*.sty)")
        if filename: self.output_input.setText(filename)

    def run_assembly(self):
        template = self.template_input.text()
        custom = self.custom_input.text()
        output = self.output_input.text()
        markers = [m.strip() for m in self.marker_input.text().split(",")]
        ratio_mode = self.ratio_combo.currentText()
        inject_reset = self.inject_reset_cb.isChecked()
        reset_hb = self.reset_hb_cb.isChecked()

        if not template or not custom or not output:
            QMessageBox.critical(self, "Error", "Harap isi semua kolom file!")
            return

        self.status_label.setText("Status: Memproses assembly dan filter...")
        self.status_label.setStyleSheet("color: #f39c12;")
        QApplication.processEvents()

        success, msg = process_assembly(template, custom, output, markers, ratio_mode, inject_reset, reset_hb)
        
        if success:
            self.status_label.setText(f"Status: {msg}")
            self.status_label.setStyleSheet(f"color: {self.FG_GREEN};")
            QMessageBox.information(self, "Berhasil", f"File berhasil disimpan di:\n{output}")
        else:
            self.status_label.setText("Status: Gagal memproses file")
            self.status_label.setStyleSheet("color: #c0392b;")
            QMessageBox.critical(self, "Gagal", f"Terjadi Error:\n{msg}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = StyleAssemblerApp()
    window.show()
    sys.exit(app.exec())
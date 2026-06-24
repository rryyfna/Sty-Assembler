import os
import mido
import sys
from mido import MidiFile, MidiTrack, Message, MetaMessage
import io
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QLineEdit, QScrollArea,
                             QFrame, QFileDialog, QMessageBox, QGridLayout)
from PyQt6.QtGui import QFont, QColor, QCursor
from PyQt6.QtCore import Qt

# --- CASM HELPER FUNCTIONS ---
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

# --- KELAS LOGIKA ASSEMBLY ---
class UniversalStyleAssembler:
    def __init__(self, base_style_path):
        self.base_style_path = base_style_path
        self.midi_bytes = b""
        
        self.base_casm_block = b""
        self.base_trailer = b""
        self.base_csegs = []
        
        self.setup_bar = []
        self.base_variations = {}
        self.base_midi = None
        self.cached_casms = {} # source_path -> csegs
        
        self._split_binary()
        self._parse_base_midi()

    def _split_binary(self):
        with open(self.base_style_path, 'rb') as f:
            data = f.read()
        casm_index = data.find(b'CASM')
        if casm_index != -1:
            self.midi_bytes = data[:casm_index]
            self.base_casm_block, self.base_trailer = extract_casm_full(self.base_style_path)
            self.base_csegs = parse_casm(self.base_casm_block) if self.base_casm_block else []
        else:
            self.midi_bytes = data
            self.base_casm_block = b""
            self.base_trailer = b""
            self.base_csegs = []

    def _parse_base_midi(self):
        midi_io = io.BytesIO(self.midi_bytes)
        self.base_midi = MidiFile(file=midi_io)
        current_variation = "Setup"
        self.setup_bar = []
        self.base_variations = {}
        
        for msg in self.base_midi.tracks[0]:
            if msg.type == 'marker':
                current_variation = msg.text
                if current_variation not in self.base_variations:
                    self.base_variations[current_variation] = []
            
            if current_variation == "Setup":
                self.setup_bar.append(msg)
            else:
                self.base_variations[current_variation].append(msg)

    def extract_source_variation(self, source_path, variation_name):
        try:
            source_midi = MidiFile(source_path)
            track = source_midi.tracks[0]
            channel_voices = {ch: {'cc0': None, 'cc32': None, 'pc': None} for ch in range(16)}
            is_recording = False
            extracted_events = []
            
            for msg in track:
                if not is_recording:
                    if msg.type == 'control_change':
                        if msg.control == 0:
                            channel_voices[msg.channel]['cc0'] = msg.value
                        elif msg.control == 32:
                            channel_voices[msg.channel]['cc32'] = msg.value
                    elif msg.type == 'program_change':
                        channel_voices[msg.channel]['pc'] = msg.program

                if msg.type == 'marker':
                    if msg.text == variation_name:
                        is_recording = True
                        start_marker = msg.copy(time=0)
                        extracted_events.append(start_marker)
                        
                        for ch, voice in channel_voices.items():
                            if voice['cc0'] is not None:
                                extracted_events.append(Message('control_change', channel=ch, control=0, value=voice['cc0'], time=0))
                            if voice['cc32'] is not None:
                                extracted_events.append(Message('control_change', channel=ch, control=32, value=voice['cc32'], time=0))
                            if voice['pc'] is not None:
                                extracted_events.append(Message('program_change', channel=ch, program=voice['pc'], time=0))
                        continue
                    elif is_recording:
                        tail_event = MetaMessage('text', text='bar_tail', time=msg.time)
                        extracted_events.append(tail_event)
                        break 
                        
                elif msg.type == 'end_of_track' and is_recording:
                    tail_event = MetaMessage('text', text='bar_tail', time=msg.time)
                    extracted_events.append(tail_event)
                    break
                        
                if is_recording:
                    extracted_events.append(msg)
                    
            return extracted_events
        except Exception as e:
            print(f"Error reading {source_path}: {e}")
            return []

    def _get_source_csegs(self, source_path):
        if source_path not in self.cached_casms:
            casm_block, _ = extract_casm_full(source_path)
            self.cached_casms[source_path] = parse_casm(casm_block) if casm_block else []
        return self.cached_casms[source_path]

    def assemble(self, mapping, output_path, all_variations):
        new_midi = MidiFile(type=0, ticks_per_beat=self.base_midi.ticks_per_beat)
        new_track = MidiTrack()
        new_midi.tracks.append(new_track)
        
        for msg in self.setup_bar:
            new_track.append(msg)
            
        final_csegs = list(self.base_csegs)
            
        for target_var in all_variations:
            if target_var in mapping:
                source_file, source_var = mapping[target_var]
                
                # --- 1. MIDI DATA ---
                events = self.extract_source_variation(source_file, source_var)
                if events and events[0].type == 'marker':
                    events[0].text = target_var 
                for msg in events:
                    new_track.append(msg)
                
                # --- 2. CASM DATA ---
                source_csegs = self._get_source_csegs(source_file)
                src_cseg = next((c for c in source_csegs if c.get('name') == source_var), None)
                if src_cseg:
                    final_csegs = [c for c in final_csegs if c.get('name') != target_var]
                    new_cseg = rename_cseg(src_cseg, target_var)
                    final_csegs.append(new_cseg)
                    
            elif target_var in self.base_variations:
                for msg in self.base_variations[target_var]:
                    new_track.append(msg)
                
        out_buffer = io.BytesIO()
        new_midi.save(file=out_buffer)
        
        final_casm_data = build_casm(final_csegs) + self.base_trailer if final_csegs else self.base_casm_block + self.base_trailer
        final_data = out_buffer.getvalue() + final_casm_data
        
        with open(output_path, 'wb') as f:
            f.write(final_data)

# --- GUI PYQT6 ---
class YamahaStyleGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("rryystudios @rryynaufal")
        self.resize(1000, 700)
        
        self.BG_MAIN = "#121212"
        self.BG_PANEL = "#1E1E1E"
        self.BG_MENU = "#080808"
        self.FG_GREEN = "#39FF14"
        self.ACTIVE_GREEN = "#1FA31F" 
        self.BTN_RADIUS = 8
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
            QPushButton {{
                color: white;
                border-radius: 4px;
                padding: 5px;
            }}
            QScrollArea {{ border: none; background-color: transparent; }}
            QScrollArea > QWidget > QWidget {{ background-color: transparent; }}
        """)
        
        self.actual_base_paths = [] 
        self.actual_master_path = ""
        self.actual_source_paths = {} 
        self.source_var_inputs = {}
        
        self.all_variations = [
            "Intro A", "Intro B", "Intro C",
            "Main A", "Main B", "Main C", "Main D",
            "Fill In AA", "Fill In BB", "Fill In CC", "Fill In DD",
            "Break",
            "Ending A", "Ending B", "Ending C"
        ]
        
        for var in self.all_variations:
            self.actual_source_paths[var] = ""
            
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
        
        title_label = QLabel("rryystudios           ")
        title_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        top_layout.addWidget(title_label)
        
        btn_base = QPushButton("Load Base Style(s)")
        btn_base.setFixedSize(150, self.INPUT_HEIGHT)
        btn_base.setStyleSheet("QPushButton { background-color: #333; } QPushButton:hover { background-color: #555; }")
        btn_base.clicked.connect(self.browse_base)
        top_layout.addWidget(btn_base)
        
        self.lbl_base_path = QLabel("[Pilih Base Style...]")
        font_italic = QFont("Arial", 10)
        font_italic.setItalic(True)
        self.lbl_base_path.setFont(font_italic)
        self.lbl_base_path.setStyleSheet("color: #AAAAAA;")
        self.lbl_base_path.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_base_path.mousePressEvent = self.show_selected_files
        top_layout.addWidget(self.lbl_base_path)
        
        top_layout.addStretch()
        
        btn_save = QPushButton("SAVE")
        btn_save.setFixedSize(120, 35)
        btn_save.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        btn_save.setStyleSheet(f"""
            QPushButton {{ background-color: #228B22; border-radius: 8px; }}
            QPushButton:hover {{ background-color: #32CD32; }}
        """)
        btn_save.clicked.connect(self.process_assembly)
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
        right_layout.setContentsMargins(20, 10, 20, 10)
        right_layout.setSpacing(10)
        
        quick_frame = QFrame()
        quick_layout = QHBoxLayout(quick_frame)
        quick_layout.setContentsMargins(0, 0, 0, 0)
        
        lbl_copy = QLabel("Copy From")
        lbl_copy.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        quick_layout.addWidget(lbl_copy)
        quick_layout.addStretch()
        
        btn_apply_all = QPushButton("Terapkan ke Semua ⬇")
        btn_apply_all.setFixedSize(160, self.INPUT_HEIGHT)
        btn_apply_all.setStyleSheet("QPushButton { background-color: #444; } QPushButton:hover { background-color: #666; }")
        btn_apply_all.clicked.connect(self.apply_to_all)
        quick_layout.addWidget(btn_apply_all)
        
        btn_master = QPushButton("Pilih Master File")
        btn_master.setFixedSize(140, self.INPUT_HEIGHT)
        btn_master.setStyleSheet("QPushButton { background-color: #333; } QPushButton:hover { background-color: #555; }")
        btn_master.clicked.connect(self.browse_master)
        quick_layout.addWidget(btn_master)
        
        right_layout.addWidget(quick_frame)

        # TABLE HEADER
        table_header = QFrame()
        table_header.setFixedHeight(40)
        table_header.setStyleSheet(f"background-color: {self.BG_PANEL}; border-radius: {self.BTN_RADIUS}px;")
        header_layout = QHBoxLayout(table_header)
        header_layout.setContentsMargins(10, 5, 10, 5)
        
        lbl_h_tgt = QLabel("Target")
        lbl_h_tgt.setFixedWidth(100)
        lbl_h_tgt.setStyleSheet("color: #888; font-weight: bold;")
        header_layout.addWidget(lbl_h_tgt)
        
        lbl_h_src = QLabel("Source File")
        lbl_h_src.setStyleSheet("color: #888; font-weight: bold;")
        header_layout.addWidget(lbl_h_src, stretch=1)
        
        lbl_h_btn = QLabel("")
        lbl_h_btn.setFixedWidth(50)
        header_layout.addWidget(lbl_h_btn)
        
        lbl_h_var = QLabel("Source Var")
        lbl_h_var.setFixedWidth(130)
        lbl_h_var.setStyleSheet("color: #888; font-weight: bold;")
        header_layout.addWidget(lbl_h_var)
        
        lbl_h_clr = QLabel("Clear")
        lbl_h_clr.setFixedWidth(60)
        lbl_h_clr.setStyleSheet("color: #888; font-weight: bold;")
        header_layout.addWidget(lbl_h_clr)
        
        right_layout.addWidget(table_header)

        # SCROLL AREA FOR ROWS
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(10)
        
        self.row_widgets = {}

        for var_name in self.all_variations:
            row_frame = QFrame()
            row_layout = QHBoxLayout(row_frame)
            row_layout.setContentsMargins(10, 0, 10, 0)
            
            tgt_lbl = QLabel(var_name)
            tgt_lbl.setFixedSize(100, 32)
            tgt_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tgt_lbl.setFont(QFont("Arial", 10, QFont.Weight.Bold))
            tgt_lbl.setStyleSheet(f"background-color: {self.FG_GREEN}; color: black; border-radius: 6px;")
            row_layout.addWidget(tgt_lbl)
            
            src_lbl = QLabel("")
            src_lbl.setFixedHeight(32)
            src_lbl.setStyleSheet("background-color: #181818; padding-left: 10px; border-radius: 4px;")
            row_layout.addWidget(src_lbl, stretch=1)
            
            btn_brw = QPushButton("...")
            btn_brw.setFixedSize(40, 32)
            btn_brw.setStyleSheet("background-color: #333; border-radius: 4px;")
            btn_brw.clicked.connect(lambda checked, v=var_name: self.browse_source_for(v))
            row_layout.addWidget(btn_brw)
            
            src_var_in = QLineEdit(var_name)
            src_var_in.setFixedSize(130, 32)
            row_layout.addWidget(src_var_in)
            
            btn_clr = QPushButton("X")
            btn_clr.setFixedSize(60, 32)
            btn_clr.setStyleSheet("background-color: #8B0000; border-radius: 4px;")
            btn_clr.clicked.connect(lambda checked, v=var_name: self.clear_source_for(v))
            row_layout.addWidget(btn_clr)
            
            scroll_layout.addWidget(row_frame)
            
            self.row_widgets[var_name] = {
                'lbl': src_lbl,
                'var': src_var_in
            }
            
        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        right_layout.addWidget(scroll_area)

        body_layout.addWidget(right_area)
        main_layout.addWidget(body_frame)

    def browse_base(self):
        filenames, _ = QFileDialog.getOpenFileNames(self, "Pilih Base Style", "", "Yamaha Style (*.sty);;All Files (*.*)")
        if filenames:
            self.actual_base_paths = list(filenames)
            disp = f"{len(filenames)} file terpilih" if len(filenames) > 1 else os.path.basename(filenames[0])
            self.lbl_base_path.setText(disp)

    def show_selected_files(self, event):
        if not self.actual_base_paths:
            return
        msg = "\n".join([os.path.basename(f) for f in self.actual_base_paths])
        QMessageBox.information(self, "Base Files", f"File Base Terpilih:\n\n{msg}")

    def browse_master(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Pilih Master Source", "", "MIDI/Style (*.mid *.sty)")
        if filename:
            self.actual_master_path = filename
            QMessageBox.information(self, "Info", f"Master diset ke:\n{os.path.basename(filename)}")

    def apply_to_all(self):
        if not self.actual_master_path:
            QMessageBox.warning(self, "Warning", "Pilih Master File terlebih dahulu!")
            return
        disp_name = os.path.basename(self.actual_master_path)
        for var_name in self.all_variations:
            self.actual_source_paths[var_name] = self.actual_master_path
            self.row_widgets[var_name]['lbl'].setText(disp_name)

    def browse_source_for(self, var_name):
        filename, _ = QFileDialog.getOpenFileName(self, f"Pilih Source untuk {var_name}", "", "MIDI/Style (*.mid *.sty)")
        if filename:
            self.actual_source_paths[var_name] = filename
            self.row_widgets[var_name]['lbl'].setText(os.path.basename(filename))

    def clear_source_for(self, var_name):
        self.actual_source_paths[var_name] = ""
        self.row_widgets[var_name]['lbl'].setText("")
        self.row_widgets[var_name]['var'].setText(var_name)

    def process_assembly(self):
        if not self.actual_base_paths:
            QMessageBox.critical(self, "Error", "Pilih Base Style terlebih dahulu!")
            return
            
        mapping = {}
        for var in self.all_variations:
            src = self.actual_source_paths[var]
            if src:
                src_var = self.row_widgets[var]['var'].text().strip()
                mapping[var] = (src, src_var)
                
        if not mapping:
            QMessageBox.warning(self, "Warning", "Tidak ada mapping yang diisi!")
            return

        out_dir = QFileDialog.getExistingDirectory(self, "Pilih Folder Penyimpanan")
        if not out_dir:
            return

        success_count = 0
        QApplication.processEvents()
        
        for base_path in self.actual_base_paths:
            try:
                assembler = UniversalStyleAssembler(base_path)
                
                base_name = os.path.basename(base_path)
                name_without_ext = os.path.splitext(base_name)[0]
                out_path = os.path.join(out_dir, f"{name_without_ext}_assembled.sty")
                
                assembler.assemble(mapping, out_path, self.all_variations)
                success_count += 1
            except Exception as e:
                print(f"Failed processing {base_path}: {e}")

        QMessageBox.information(self, "Selesai", f"Berhasil memproses {success_count} file!\nTersimpan di:\n{out_dir}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = YamahaStyleGUI()
    window.show()
    sys.exit(app.exec())

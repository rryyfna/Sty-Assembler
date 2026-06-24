# Yamaha Style Assembler

Aplikasi berbasis Python dengan antarmuka CustomTkinter untuk memproses dan menggabungkan file style arranger Yamaha (.sty).

Aplikasi ini menyediakan solusi untuk melakukan *assembly* atau perakitan file style secara cepat melalui komputer, tanpa perlu melakukan proses perakitan secara langsung pada keyboard arranger. Sistem akan menangani penggabungan data MIDI dan pemetaan *chunk* CASM sehingga file style keluaran dapat berjalan dengan normal beserta seluruh parameter CASM-nya.

## Fitur Utama
- Ekstraksi dan injeksi bagian CASM secara tepat.
- Pemetaan penanda bagian style (*marker*), misalnya `Main A` ke `Main B`.
- Sinkronisasi panjang birama secara otomatis berdasarkan style sumber.
- Antarmuka grafis (GUI) dengan tampilan mode gelap (*dark mode*).

## Instalasi
```bash
pip install -r requirements.txt
```

## Penggunaan
```bash
python main.py
```

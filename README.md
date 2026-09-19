# Journal Administration System (JAS)

JAS adalah aplikasi administrasi jurnal akademik berbasis Streamlit. Sistem ini melengkapi OJS tanpa mengubah core OJS, mendukung banyak jurnal, dan memisahkan keputusan editorial dari urusan pembayaran dan publikasi.

Implementasi yang tersedia meliputi:

- autentikasi PBKDF2-SHA256, role-based access, dan isolasi data per jurnal;
- konfigurasi multi-jurnal dengan placeholder ELKOLIND dan JASENS;
- input, edit, pencarian, filter, serta impor submission dari CSV/Excel;
- workflow editorial dan publikasi yang tervalidasi dan terpisah;
- LoA A4 dengan QR, revoke, reissue, dan version history tanpa overwrite;
- invoice A4, formula total tervalidasi, status finansial, dan tautan penulis bertoken aman;
- unggahan bukti pembayaran dengan allowlist, pemeriksaan signature file, batas ukuran, dan nama acak;
- verifikasi/reject pembayaran, receipt A4, dan QR verifikasi publik;
- dashboard, issue monitor, global search, Excel workbook enam laporan, dan audit log;
- migration PostgreSQL, Docker Compose, serta fallback SQLite khusus pengembangan;
- abstraction OJS yang secara eksplisit belum aktif sampai endpoint dan kredensial diverifikasi.

## Arsitektur

```text
app.py                         UI Streamlit, public routes, page orchestration
config.py                      environment/Streamlit Secrets configuration
models/
  base.py                      UUID portability and SQLAlchemy base
  enums.py                     domain states and roles
  entities.py                  relational data model and constraints
services/
  database.py                  engine, transactions, schema initialization
  core.py                      auth, authorization, workflow, numbering,
                               PDF/QR, payment, verification, reports, audit
  ojs_service.py               future OJS adapter contract
migrations/001_initial_postgresql.sql
scripts/init_db.py
tests/
```

UI, konfigurasi, persistence, model domain, business rules, pembuatan dokumen, dan integrasi eksternal tidak dicampur dalam satu lapisan.

## Relasi entitas

```text
journals 1---* submissions 1---* authors
    |              |
    |              +---* loa_documents ---1 document_verifications
    |              +---* invoices --------1 document_verifications
    |                        |
    |                        +---* payments ---0..1 receipts ---1 document_verifications
    |
    +---* user_journals *---1 users
    +---* document_sequences
    +---* audit_logs
```

Foreign key `RESTRICT` mencegah penghapusan objek finansial/dokumen yang masih direferensikan. Pembatalan memakai status `CANCELLED` atau `REVOKED`, bukan delete. `journal_id + ojs_submission_id` unik, begitu pula nomor dokumen, token verifikasi, dan scope sequence per jurnal/jenis/tahun.

## State transitions

Editorial:

```text
SUBMITTED -> UNDER_REVIEW -> ACCEPTED
                         -> REJECTED
SUBMITTED/UNDER_REVIEW -> WITHDRAWN
```

Financial:

```text
WAITING_PAYMENT -> PAYMENT_SUBMITTED -> PAID
                                  \-> REJECTED -> WAITING_PAYMENT
ISSUED/WAITING_PAYMENT/PAYMENT_SUBMITTED -> CANCELLED
```

Publication:

```text
NOT_READY <-> READY_FOR_PUBLICATION -> PUBLISHED
```

Tidak ada transisi pembayaran yang mengubah `editorial_status`. LoA dan invoice mensyaratkan submission `ACCEPTED`; receipt mensyaratkan payment `VERIFIED`.

## Konfigurasi wajib

Salin `.env.example` ke `.env` untuk Docker Compose, atau `.streamlit/secrets.toml.example` ke `.streamlit/secrets.toml` untuk menjalankan Streamlit langsung. Jangan commit file rahasia.

| Variable | Tujuan |
|---|---|
| `DATABASE_URL` | URL PostgreSQL SQLAlchemy; SQLite hanya fallback lokal |
| `PUBLIC_BASE_URL` | basis URL untuk QR dan tautan publik |
| `ADMIN_EMAIL` | email bootstrap Super Admin |
| `ADMIN_PASSWORD` | password bootstrap minimal 12 karakter; hapus setelah akun dibuat |
| `ADMIN_NAME` | nama Super Admin |
| `PRIVATE_UPLOAD_DIR` | penyimpanan bukti bayar non-public |
| `DOCUMENT_OUTPUT_DIR` | penyimpanan dokumen PDF |
| `MAX_UPLOAD_BYTES` | batas file upload, default 5 MB |

JAS tidak menyimpan password database, service key, password admin, atau API key di source code.

## Menjalankan secara lokal

Persyaratan: Python 3.12 dan PostgreSQL 14+ untuk deployment production.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
# Edit secrets.toml sebelum langkah berikutnya.
python scripts\init_db.py
streamlit run app.py
```

Untuk percobaan tanpa PostgreSQL, hilangkan `DATABASE_URL`; aplikasi membuat `jas.db`. Mode ini diberi peringatan di sidebar dan tidak disarankan untuk concurrency production.

## PostgreSQL dan migration

Untuk deployment baru, buat database kosong lalu jalankan:

```powershell
psql "$env:DATABASE_URL" -f migrations\001_initial_postgresql.sql
python scripts\init_db.py
```

`scripts/init_db.py` bersifat idempotent untuk tabel SQLAlchemy, placeholder jurnal, dan akun bootstrap. PostgreSQL menggunakan atomic `INSERT ... ON CONFLICT ... RETURNING` untuk nomor LoA/invoice/receipt, bukan `COUNT(records) + 1`.

## Docker Compose

Isi `.env` setidaknya dengan `POSTGRES_PASSWORD`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, dan `PUBLIC_BASE_URL`, lalu:

```powershell
docker compose up --build
```

PDF dan bukti bayar berada di named volume `jas_files`; database berada di `jas_postgres`. Gunakan TLS reverse proxy dan backup terenkripsi pada production.

## Tautan publik

QR dokumen menghasilkan `/verify/{token}` dan tautan pembayaran menghasilkan `/payment/{token}`. Pada deployment Streamlit, reverse proxy harus meneruskan kedua path tersebut ke aplikasi tanpa menghapus path. Query fallback juga didukung:

- `/?verify={token}`
- `/?payment_token={token}`

Halaman verifikasi hanya menampilkan jenis/nomor/status dokumen, jurnal, judul, penulis, dan tanggal. Email, detail bank, nilai pembayaran, serta bukti unggahan tidak ditampilkan.

Raw token pembayaran hanya ditampilkan satu kali ketika invoice diterbitkan; database hanya menyimpan SHA-256 hash dan waktu kedaluwarsa. Jika tautan hilang, invoice perlu dibatalkan dan diterbitkan ulang pada versi berikutnya (fitur rotasi token terpisah dapat ditambahkan dengan audit trail).

## Testing

```powershell
pytest -q
```

Tes mencakup hashing password, format Rupiah, upload/versioning/activation Master DOCX, replacement ELKOLIND dan JASENS beserta identitas Editor-in-Chief statis, bulan Romawi IX–XII dan rollover tahun, preview tanpa konsumsi nomor, state transition, serta lifecycle LoA–invoice–payment–receipt pada SQLite terisolasi.

Langkah smoke test manual:

1. Masuk sebagai Super Admin dan lengkapi identitas jurnal di Settings.
2. Buat Journal Admin serta Finance, lalu assign jurnal.
3. Tambah submission; ubah `SUBMITTED -> UNDER_REVIEW -> ACCEPTED`.
4. Upload/aktifkan Master LoA per jurnal, generate preview, pastikan nomor belum bertambah, issue LoA, buka verification, reissue dengan alasan, dan pastikan versi lama `SUPERSEDED`.
5. Generate invoice dan simpan secure author link yang tampil satu kali.
6. Buka link dalam sesi privat, upload bukti pembayaran yang valid, lalu verifikasi sebagai Finance.
7. Generate receipt, uji QR, mark ready/published, dan export Excel.
8. Periksa Audit Log untuk seluruh critical state changes.

## Risiko keamanan dan kontrol

- **Credential exposure:** hanya env/secrets; file contoh tidak berisi kredensial nyata.
- **Broken access control:** izin dicek server-side berdasarkan role dan `user_journals`, bukan hanya menyembunyikan tombol.
- **SQL injection:** seluruh query memakai SQLAlchemy expressions dan bound parameters.
- **Upload abuse:** extension allowlist, magic-byte check, size limit, random filename, serta storage di luar static/public path.
- **Token guessing/leak:** `secrets.token_urlsafe(32)`, token pembayaran disimpan sebagai hash, token memiliki expiration.
- **Document tampering:** setiap PDF memiliki token/QR dan status publik; revoke/supersede tetap ada di audit history.
- **Data loss:** financial records tidak dihapus; gunakan backup PostgreSQL dan object storage dengan versioning.
- **Session risk:** Streamlit session dipakai untuk login UI; deployment production sebaiknya menambahkan identity-aware reverse proxy/SSO, HTTPS, secure headers, rate limiting, dan centralized session revocation.
- **Path disclosure:** path internal tidak pernah ditampilkan ke penulis; file diunduh melalui handler Streamlit terautorisasi.
- **Operational gap:** pengiriman email, Supabase service role, dan OJS belum diaktifkan tanpa konfigurasi. Tidak ada fungsi dummy yang dilabeli aktif.

## OJS future integration

`services/ojs_service.py` mendefinisikan contract `get_submission`, `get_submission_metadata`, `get_authors`, dan `get_editorial_status`. Adapter default selalu mengembalikan `NotImplementedError` yang jelas. Jangan mengasumsikan endpoint atau menulis langsung ke tabel OJS. Implementasi berikutnya harus memverifikasi versi OJS, endpoint resmi, mapping status, pagination, retry, idempotency, dan audit sinkronisasi terlebih dahulu.

## Master LoA DOCX per jurnal

LoA tidak lagi digambar ulang dengan ReportLab. Setiap jurnal memiliki Master DOCX berversi di private storage dan satu versi berstatus `ACTIVE`. Mesin template mengganti hanya node teks placeholder di paket OOXML; gambar, relasi, floating objects, font, run formatting, margin, tanda tangan/stempel, dan branding resmi tetap berasal dari dokumen Word yang diunggah.

Placeholder resmi:

- ELKOLIND: `{{loa_number}}`, `{{recipient_name}}`, `{{ojs_submission_id}}`, `{{authors}}`, `{{article_title}}`, `{{volume}}`, `{{issue}}`, `{{publication_month}}`, `{{publication_year}}`, `{{loa_date}}`.
- JASENS: `{{loa_number}}`, `{{recipient_name}}`, `{{recipient_affiliation}}`, `{{article_title}}`, `{{authors}}`, `{{volume}}`, `{{issue}}`, `{{publication_month}}`, `{{publication_year}}`.
- Opsional: `{{journal_url}}`, `{{journal_email}}`, dan `{{verification_qr}}` bila penempatan QR menggunakan `template-placeholder`.

Alur penerbitan wajib: pilih submission `ACCEPTED` → `Generate Preview` → periksa PDF berlabel `DRAFT PREVIEW — NOT ISSUED` → `Issue LoA`. Preview tidak memanggil sequence engine, tidak membuat verification `VALID`, dan tidak menulis audit penerbitan. Nomor final baru dialokasikan dalam transaksi saat `Issue LoA`. Jika hasil lebih dari satu halaman, penerbitan dinonaktifkan sampai editor menyetujui overflow secara eksplisit.

Reissue tidak pernah menimpa dokumen lama. LoA lama menjadi `SUPERSEDED`, PDF/DOCX/nomor/token verifikasinya tetap ada, dan alasan reissue wajib dicatat.

### Konversi DOCX ke PDF

`DOCX_PDF_CONVERTER` menerima `auto`, `word`, `libreoffice`, atau `none`.

- Windows lokal: `auto` mencoba Microsoft Word, lalu LibreOffice bila tersedia.
- Docker/produksi: image memasang LibreOffice Writer dan Compose menetapkan `libreoffice`.
- Instalasi khusus: isi `LIBREOFFICE_PATH` dengan executable yang benar.
- Bila converter tidak tersedia, sistem mempertahankan DOCX dan menampilkan kesalahan konfigurasi beserta tombol unduh; sistem tidak mencatat PDF sebagai berhasil.

### Master template bawaan

Salinan template-kompatibel resmi tersedia di:

- `templates/loa/elkolind/v1/LoA Elkolind-2026.docx`
- `templates/loa/jasens/v1/Draft_LoA_JASENS.docx`

Jalankan `python scripts/seed_official_templates.py` setelah migrasi untuk menyalinnya ke private template storage dan mengaktifkannya. File sumber resmi tidak pernah diubah.

### Migrasi dan upgrade

Ikuti `UPGRADE_GUIDE.md`. Migrasi `scripts/migrate_master_loa.py` bersifat additive/idempotent, otomatis membuat backup SQLite, dan tidak menghapus database atau tabel lama. Untuk PostgreSQL, buat `pg_dump` lebih dahulu lalu set `JAS_MIGRATION_BACKUP_CONFIRMED=1`.

### Verifikasi visual

`VISUAL_REGRESSION.md` mencatat audit page size/margin, inventaris gambar, pemeriksaan package part, hasil render satu halaman, dan inspeksi manual sampel ELKOLIND/JASENS. Sampel final ada di `samples/`.

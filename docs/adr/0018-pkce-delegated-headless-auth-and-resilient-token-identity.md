# 18. PKCE-Delegated Headless Authentication & Resilient Token Identity

Date: 2026-11-18

## Status

Accepted

## Context

Pembaruan Antigravity CLI (`agy` 1.5.10) serta kebijakan keamanan Google OAuth memunculkan dua kegagalan mendasar pada alur `agy-switch login`:

1. **Penolakan OAuth Out-Of-Band (`urn:ietf:wg:oauth:2.0:oob`) pada Mode `--headless`**:
   `build_oauth_url()` di `scripts/agy_switcher/telemetry.py` masih membangun URL dengan `redirect_uri=urn:ietf:wg:oauth:2.0:oob` tanpa PKCE. Google memblokir flow OOB tersebut dengan `Error 400: invalid_request` (`flowName=GeneralOAuthFlow`). Sementara itu, `agy` 1.5.10 telah beralih ke flow PKCE dinamis (`code_challenge_method=S256`) dengan `redirect_uri=https://antigravity.google/oauth-callback`.
2. **Perubahan Skema `id_token` pada File Kredensial `agy` 1.5.10**:
   Pada `agy` 1.5.10, field `id_token` JWT bersifat legacy (`id_token,omitempty`) dan digantikan oleh `id_token_email` (`id_token_email,omitempty`)—bahkan kedua field dapat absen pada alur tertentu meski `token` (`access_token`, `refresh_token`, `token_type`, `expiry`) dan `auth_method` valid. `validate_token_dict()` di `scripts/agy_switcher/security.py` dan `DoctorEngine._check_token_integrity()` di `scripts/agy_switcher/doctor.py` masih mewajibkan `id_token` non-kosong sehingga menolak login baru maupun token hasil penyimpanan skema terbaru.

---

## Decision

Kami menetapkan dua keputusan arsitektural untuk menjaga kompatibilitas jangka panjang dengan `agy` CLI dan Google OAuth:

### 1. Delegasi Sesi PKCE Penuh ke Proses `agy` untuk Headless Authentication
- `agy-switch login <alias> --headless` tidak lagi membentuk URL OAuth sendiri maupun menggunakan `urn:ietf:wg:oauth:2.0:oob`. Fungsi `build_oauth_url()` berbasis OOB dihentikan.
- `login_headless()` menjalankan proses `agy` secara langsung dalam mode headless (mis. `agy -p` / sesi terisolasi), menangkap URL OAuth PKCE dinamis yang dicetak oleh `agy`, menampilkannya ke terminal, lalu meneruskan kode otorisasi dari input pengguna (`stdin` atau callback `prompt_fn` pasca-URL) ke proses `agy` yang sama.
- Flag `--auth-code` pra-eksekusi ditolak secara eksplisit dengan pesan error yang mengarahkan pengguna ke alur interaktif pasca-URL, karena `code_verifier` dan `state` PKCE dibuat dinamis per sesi oleh proses `agy`.

### 2. Kontrak Resilient Token Identity & Diagnostik Berjenjang
- **Validasi Skema (`validate_token_dict`)**: Field `id_token` tidak lagi diwajibkan ada. Token OAuth dianggap valid secara struktural selama memiliki objek `token` lengkap (`access_token`, `refresh_token`, `token_type`, `expiry`) dan `auth_method`.
- **Resolusi Email Berlapis**: Identitas email (**Token Identity**) diselesaikan berurutan dari:
  1. Claim `email` pada `id_token` JWT (jika ada dan valid),
  2. Field `id_token_email` non-kosong (skema `agy` 1.5.10),
  3. *Best-effort lookup* ke Google UserInfo API menggunakan `access_token` saat `login` atau `add` apabila keduanya tidak tersedia, lalu menyimpannya sebagai `id_token_email` di `token.json` vault.
- **Pembedaan Token Kosong vs Korup**: Ketiadaan `id_token` (`None` / tidak ada / string kosong) diterima sebagai valid. Namun, apabila field `id_token` **ada dan tidak kosong** tetapi gagal di-parse sebagai JWT (dan tidak ada `id_token_email` yang valid), token tetap ditolak sebagai korup.
- **Severity Diagnostik `doctor`**: Akun dengan `id_token_email` valid berstatus `[PASS]`. Akun OAuth valid yang sama sekali tidak memiliki identitas email (`id_token` maupun `id_token_email` absen) dilaporkan sebagai `[WARN]` non-fatal (tidak memicu `[FAIL]`), sedangkan `id_token` non-kosong yang korup tetap ditandai sebagai anomali integritas.

---

## Considered Options & Rejected Alternatives

- **Implementasi PKCE Mandiri di Python (`oauth2.googleapis.com/token`)**: Ditolak karena menduplikasi daftar OAuth scopes `agy` (`cloud-platform`, `aicode`, `cclog`, `experimentsandconfigs`, dll.), memerlukan pemeliharaan `CLIENT_SECRET` di `agy-switcher`, dan berisiko tidak sinkron jika `agy` kembali mengubah format penyimpanan token atau parameter OAuth.
- **Mewajibkan `id_token` atau Menolak Token Tanpa Email Secara Keras**: Ditolak karena `agy` 1.5.10 sudah menjadikan `id_token` bersifat opsional (`omitempty`) sehingga penolakan keras memutus fungsi dasar `login` dan `switch`.

---

## Consequences

- **Positif**: `agy-switch` menjadi tangguh terhadap evolusi skema token `agy` serta tidak pernah memancarkan URL OOB yang diblokir Google; `--headless` tetap menjaga *single source of truth* OAuth pada biner resmi `agy`.
- **Negatif / Trade-off**: Otomatisasi satu baris yang mengandalkan pre-supplied `--auth-code` tidak lagi didukung pada CLI dan harus beralih ke `login_headless(..., prompt_fn=...)` atau alur interaktif pasca-URL.

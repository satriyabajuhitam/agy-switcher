# 19. Retire Headless Login Mode and Standardize on Interactive TTY OAuth

Date: 2026-11-18

## Status

Accepted (supersedes the `--headless` login flow in [ADR-0013](file:///srv/agy-workspaces/booster/agy-switcher/docs/adr/0013-standardized-pyproject-and-in-process-sdk.md) and Decision #1 of [ADR-0018](file:///srv/agy-workspaces/booster/agy-switcher/docs/adr/0018-pkce-delegated-headless-auth-and-resilient-token-identity.md))

## Context

Pengujian empiris terhadap binary `agy` terbaru (`/home/agyuser/.local/bin/agy`) menunjukkan dua fakta yang membuat mode `agy-switch login --headless` tidak lagi layak maupun diperlukan:

1. **TUI `agy` Memecah Pipe Non-PTY**: Ketika dijalankan dengan `stdin`/`stdout`/`stderr` ter-pipe tanpa PTY oleh `AgySwitchEngine.login_headless()`, `agy` hanya memancarkan escape sequence terminal (`\x1b[?2026$p...\x1b[?1049h...`) lalu menggantung tanpa mencetak URL OAuth PKCE. Bahkan di dalam PTY, `agy` kini menampilkan menu interaktif (`Select login method: > 1. Google OAuth`) yang memerlukan penekanan `Enter` dan membungkus URL OAuth di beberapa baris TUI.
2. **Alur Standar `agy-switch login <alias>` Sudah Berfungsi di Server maupun Desktop**: `AgySwitchEngine.login_account()` mewariskan `stdin`, `stdout`, dan `stderr` langsung ke TTY terminal pengguna, sehingga TUI `agy` berjalan normal di terminal lokal maupun sesi SSH VPS (menampilkan URL OAuth saat browser tidak terbuka otomatis) lalu menyimpan token ke vault secara identik.

## Decision

Kami menghentikan seluruh fitur login `--headless` dan melakukan penghapusan bersih pada permukaan CLI dan engine-nya:

1. **Penghapusan Bersih CLI & Engine**: Menghapus flag `--headless` dan flag deprecated `--auth-code` dari `agy-switch login` (`scripts/agy_switcher/cli.py`), serta menghapus `AgySwitchEngine.login_headless()`, `_run_headless_agy_session()`, dan helper scraping PKCE stream terkait di `scripts/agy_switcher/vault.py`. Tidak ada stub atau pesan deprecation CLI yang disisakan.
2. **Satu Alur Login Resmi**: `agy-switch login <alias>` (`AgySwitchEngine.login_account()`) menjadi satu-satunya alur login interaktif resmi dengan pewarisan TTY langsung ke binary `agy`.
3. **Pelestarian Token Identity & Pensiun OOB**: Keputusan #2 pada [ADR-0018](file:///srv/agy-workspaces/booster/agy-switcher/docs/adr/0018-pkce-delegated-headless-auth-and-resilient-token-identity.md) (**Token Identity**: `id_token` opsional, resolusi berlapis via `id_token_email` dan fallback Google UserInfo API, serta diagnostik `[WARN]` di `doctor`) dan penghentian `build_oauth_url()` berbasis `urn:ietf:wg:oauth:2.0:oob` tetap dipertahankan sepenuhnya.

## Considered Options & Rejected Alternatives

- **Menulis Ulang `login_headless()` dengan PTY + Parsing ANSI TUI**: Ditolak karena sangat rapuh terhadap perubahan tata letak TUI `agy` (menu pilihan login, line-wrapping URL, kode ANSI) sementara `agy-switch login <alias>` sudah bekerja tanpa lapisan scraping apa pun.
- **Menyisakan Flag `--headless` sebagai Pesan Deprecation**: Ditolak demi menjaga CLI tetap bersih dan menghindari kebingungan pengguna bahwa login di server/VPS cukup memakai `agy-switch login <alias>`.

## Consequences

- **Positif**: Menghilangkan ketergantungan rapuh pada format stdout/TUI `agy`, menyederhanakan `vault.py` dan `cli.py`, serta menyatukan pengalaman login Desktop dan VPS pada satu perintah (`agy-switch login <alias>`).
- **Negatif / Trade-off**: Pengujian atau pemanggil Python eksternal yang sebelumnya mengandalkan `AgySwitchEngine.login_headless(..., prompt_fn=...)` tidak lagi didukung; migrasi akun terautomasi antar-mesin tetap dilayani oleh `vault export` / `vault import`.

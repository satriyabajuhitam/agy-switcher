# 20. Scoped Lifecycle Hooks, Workspace Guard, and PreInvocation Latency Optimization

Date: 2026-11-19

## Status

Accepted

## Context

Plugin `agy-switch` dipasang secara global pada sistem Antigravity pengguna (`~/.gemini/config/plugins/agy-switch/` dan launcher `~/.local/bin/agy-switch`).
Dalam konfigurasi `hooks.json` bawaan plugin dan file global `~/.gemini/config/hooks.json`, hook `Stop` bernama `build-quality-gate` terdaftar aktif (`"enabled": true`) dengan mengeksekusi:
```bash
bash /srv/agy-workspaces/booster/agy-switcher/scripts/verify-build.sh
```
Karena terdaftar secara global atau pada level plugin, Antigravity mengeksekusi hook `Stop` ini di **setiap akhir pesan (turn stop event) di semua project dan percakapan pengguna**, menyebabkan UI mengalami jeda/spinner ("working") hingga beberapa detik karena menjalankan pemeriksaan arsitektur AST, `mypy --strict`, dan seluruh unit test suite `agy-switcher` di sembarang repository.

Selain itu, hook `PreInvocation` (`agy-switch-auto-rotate`) yang mengeksekusi `scripts/agy_switch.py hook-pre-invocation` sebelum setiap turn inferensi model saat ini memakan waktu rata-rata ~108ms karena mengimpor seluruh modul (`models`, `security`, `telemetry`, `rotation`, `doctor`, `vault`, `hook`) pada saat cold start, padahal target latensi fast-path turn adalah <40ms.

## Decision

Kami menetapkan arsitektur **Defense-in-Depth** untuk membatasi eksekusi lifecycle hook agar tidak membebani project eksternal:

1. **Nonaktifkan `build-quality-gate` di Distribusi Plugin (`"enabled": false`)**:
   Di dalam `hooks.json` repositori maupun distribusi plugin, entri `build-quality-gate` disetel ke `"enabled": false` secara default. Kualitas build selama development lokal dapat diaktifkan secara opt-in melalui hook workspace lokal (`.agents/hooks.json`) atau dipanggil manual/CI.
2. **Workspace Guard pada `scripts/verify-build.sh`**:
   Tambahkan pemeriksaan lingkungan di awal `scripts/verify-build.sh`. Script akan membaca input `stdin` (payload JSON hook Antigravity yang memuat `cwd` dan `workspacePaths`) atau `$PWD`. Jika eksekusi tidak berasal dari atau menargetkan repositori `agy-switcher` (misalnya tidak ada marker `pyproject.toml` dengan nama `agy-switch` atau direktori kerja di luar repo), script langsung melakukan `exit 0` (no-op) dalam <5ms tanpa menjalankan AST check, mypy, maupun unit tests.
3. **Pembersihan `hooks.json` Global oleh `install.sh` & `vault.py`**:
   `AgySwitchEngine.install_hook()` dan skrip installer `install.sh` hanya mendaftarkan dan memelihara hook `agy-switch-auto-rotate` di `~/.gemini/config/hooks.json`. Segala entri stale `build-quality-gate` pada `~/.gemini/config/hooks.json` akan otomatis dibersihkan (dipangkas) saat instalasi atau pembaruan plugin.
4. **Optimasi Lazy-Import pada `hook-pre-invocation`**:
   Restrukturisasi alur startup `scripts/agy_switch.py` untuk sub-perintah `hook-pre-invocation`. Pemuatan modul berat (`doctor`, `telemetry`, `formatters`) ditangguhkan (lazy import), sehingga fast-path pemeriksaan cache Tier 1 (<5 menit) dieksekusi dalam <35ms.

## Considered Options & Rejected Alternatives

- **Hanya Mengandalkan Workspace Guard (Membiarkan `"enabled": true`)**: Ditolak karena tetap memicu overhead spawn subproses bash (~5-10ms) dan pembacaan pipe pada setiap akhir turn di seluruh project luar.
- **Menghapus Total `build-quality-gate` dari `hooks.json`**: Ditolak karena struktur hook `build-quality-gate` tetap bermanfaat sebagai referensi dan konfigurasi siap-pakai bagi pengembang `agy-switcher` yang ingin mengaktifkannya di workspace lokal.
- **Membatasi `PreInvocation` Hanya untuk Workspace Tertentu**: Ditolak karena auto-rotate akun merupakan fitur inti global yang harus melindungi pengguna dari quota exhaustion di semua repositori kerja mereka.

## Consequences

- **Positif**:
  - Menghilangkan sepenuhnya UI freeze/delay saat turn selesai di workspace project lain.
  - Mempercepat fast-path PreInvocation menjadi <40ms.
  - Menjamin `~/.gemini/config/hooks.json` tetap bersih dari hook internal dev.
- **Negatif / Trade-off**:
  - Developer `agy-switcher` yang ingin verifikasi build otomatis pada setiap akhir turn harus mengaktifkan `"enabled": true` di level workspace lokal mereka sendiri.

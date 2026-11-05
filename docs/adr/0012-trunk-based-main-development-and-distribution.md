# 12. Trunk-Based Development and Distribution via Main Branch

Date: 2026-11-05

## Status

Accepted

## Context

Pada arsitektur awal dan pasca ADR-0004, `agy-switch` mengadopsi model percabangan mirip Gitflow:
1. Branch `dev` berfungsi sebagai *long-lived integration branch* utama.
2. Aturan operasional repository di [`AGENTS.md`](file:///srv/agy-workspaces/booster/agy-switcher/AGENTS.md) secara ketat melarang commit atau modifikasi langsung pada `main`:
   > *"5. **Branching & Git Workflow**: All feature development and modifications must occur on the `dev` branch. Direct commits or pushes to `main` are strictly forbidden; `main` is reserved only for tested, stable releases via merge."*
3. Skrip installer remote bootstrap [`install.sh`](file:///srv/agy-workspaces/booster/agy-switcher/install.sh) meng-hardcode default branch clone ke `dev`:
   ```bash
   BRANCH="${AGY_SWITCH_BRANCH:-dev}"
   ```
4. Dokumentasi pada `README.md` dan rangkaian unit test di [`tests/test_installer.py`](file:///srv/agy-workspaces/booster/agy-switcher/tests/test_installer.py) secara deterministik mengunci branch `dev` sebagai branch default.

Setelah Pull Request #1 dimerge ke `main` dan repositori memasuki tahap publik yang stabil, pemeliharaan branch `dev` yang terpisah menimbulkan gesekan operasional:
- *Branch drift* dan *double-merge overhead* antara `dev` dan `main`.
- Kerancuan bagi pengguna publik GitHub di mana default branch repositori adalah `main`.
- Upaya menghapus branch `dev` pasca-merge langsung menggagalkan *Quality Gate* karena installer dan pengujian depend pada eksistensi branch `dev`.

## Decision

Kami memutuskan untuk beralih sepenuhnya ke model **Trunk-Based Development** dengan `main` sebagai pusat pengembangan dan distribusi:

1. **Trunk-Based Workflow**:
   - Menetapkan branch `main` sebagai satu-satunya *long-lived branch* (trunk).
   - Seluruh pengembangan fitur dan perbaikan bug baru dilakukan via *short-lived branches* (`feat/*`, `fix/*`) atau commit terverifikasi langsung ke `main` yang dilindungi oleh eksekusi test suite lokal sebelum push.
   - Merevisi Aturan Operasional #5 pada [`AGENTS.md`](file:///srv/agy-workspaces/booster/agy-switcher/AGENTS.md) agar selaras dengan model ini.

2. **Unified Distribution Default ke `main`**:
   - Memperbarui skrip bootstrap [`install.sh`](file:///srv/agy-workspaces/booster/agy-switcher/install.sh):
     ```bash
     BRANCH="${AGY_SWITCH_BRANCH:-main}"
     ```
   - Memperbarui dokumentasi [`README.md`](file:///srv/agy-workspaces/booster/agy-switcher/README.md) agar seluruh tautan `curl` dan tabel parameter mengarah ke `main`.

3. **Refaktor Test Suite**:
   - Memperbarui [`tests/test_installer.py`](file:///srv/agy-workspaces/booster/agy-switcher/tests/test_installer.py) agar metode uji bootstrap memvalidasi kloning default ke branch `main`.

4. **Retirement Branch `dev`**:
   - Menghapus branch `dev` secara permanen baik pada repositori lokal maupun remote (`origin/dev`) setelah seluruh perubahan di atas diverifikasi hijau (100% test pass).

## Consequences

**Positif:**
- Repositori memiliki *single source of truth* yang bersih dan tidak ambigu.
- Pengguna publik dapat menginstal versi stabil langsung dari `main` tanpa ketergantungan pada branch `dev`.
- Menghilangkan redundansi pemeliharaan branch `dev` dan mencegah kegagalan pipeline CI/test runner.

**Negatif / Trade-offs:**
- Karena branch `main` kini menjadi target kloning publik sekaligus basis pengembangan, setiap commit yang masuk ke `main` wajib lolos seluruh 741+ pengujian unit secara ketat untuk mencegah rusaknya skrip bootstrap bagi pengguna baru.

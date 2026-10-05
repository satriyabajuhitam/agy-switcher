# Auto-Rotate Account Research & Architectural Blueprint

> **Primary Sources:**
> - [polystree/openag Repository](https://github.com/polystree/openag)
> - [Antigravity Lifecycle Hooks Specification](file:///home/agyuser/.gemini/antigravity/builtin/skills/agy-customizations/docs/hooks.md)
> - [Google Cloud Code Internal Quota API (`/v1internal:retrieveUserQuotaSummary`)](https://cloudcode-pa.googleapis.com)
> - [agy-switch Codebase & Directives](file:///srv/agy-workspaces/booster/agy-switcher/scripts/agy_switch.py)

---

## 1. Executive Summary

Proyek [`polystree/openag`](https://github.com/polystree/openag) adalah ekstensi VS Code / Antigravity IDE berbasis TypeScript yang mengimplementasikan *multi-account quota pool* dan *auto-rotation* untuk Google Antigravity. Solusi ini menyelesaikan kendala habisnya kuota model (Gemini 5-hour limit, Claude 5-hour limit, serta weekly limit) dengan cara merutekan akun secara dinamis ke akun yang memiliki sisa kuota terbesar tanpa memerlukan proxy eksternal atau server perantara.

Tujuan riset ini adalah membedah arsitektur mekanis OpenAG dan merancang blueprint arsitektur yang ekuivalen untuk repositori **`agy-switch`** (Antigravity Multi-Account Switcher & Vault Manager), dengan tetap mematuhi seluruh *Standing Directives* repositori kita:
1. **Zero External Pip Dependencies**: Menggunakan murni Python 3.10+ standard library dan POSIX bash.
2. **Dual-Destination Atomic Swaps**: Pembaruan token dilakukan atomik (`os.replace`) ke Desktop (`~/.gemini/jetski-standalone-oauth-token`) dan CLI (`~/.gemini/antigravity-cli/antigravity-oauth-token`).
3. **Turn Safety & Process Supervisor**: Memastikan keselamatan giliran aktif (tidak mematikan `language_server` saat turn aktif).
4. **Absolute Credential Protection**: Menjaga perizinan berkas `0600` dan direktori `0700` di vault `~/.antigravity-vault/`.

---

## 2. Deep-Dive Architecture & Core Mechanics of OpenAG

Analisis terhadap source code OpenAG (`src/core/*`, `src/hook.ts`, `src/extension.ts`) mengungkap 4 pilar utama cara kerja auto-rotation:

```
+---------------------------------------------------------------------------------------+
|                                    OpenAG Architecture                                |
+---------------------------------------------------------------------------------------+
|                                                                                       |
|   1. Antigravity IDE Hook                2. Direct Quota Telemetry (CloudCode API)   |
|   +--------------------------+           +----------------------------------------+   |
|   | ~/.gemini/config/        |           | POST /v1internal:retrieveUserQuotaSummary|   |
|   | hooks.json               |           | Bearer <Access Token>                  |   |
|   | (PreInvocation Hook)     |           | -> Gemini & Claude 5h/Weekly Buckets   |   |
|   +------------+-------------+           +-------------------+--------------------+   |
|                |                                             |                        |
|                v (stdin/stdout)                              v                        |
|   +------------+-------------+           +-------------------+--------------------+   |
|   | dist/hook.js             |           | QuotaMonitor (Cache & Poller)          |   |
|   | -> HTTP POST :27182      |           | Concurrency: 4, Interval: 60s/300s     |   |
|   +------------+-------------+           +-------------------+--------------------+   |
|                |                                             |                        |
|                +--------------------+  +---------------------+                        |
|                                     |  |                                              |
|                                     v  v                                              |
|                      +------------------------------+                                 |
|                      | TokenManager.autoSelect      |                                 |
|                      | HighestQuota (Algorithms)    |                                 |
|                      +--------------+---------------+                                 |
|                                     |                                                 |
|                                     v                                                 |
|   3. Rotation Strategies & Heuristics    4. Token Sync & Reactive Intercept          |
|   - auto_highest (+Tier weight)          - USSBridge (antigravityUnifiedStateSync)    |
|   - cache_optimized (sticky >15%)        - Transcript Watcher (UsageTracker)          |
|   - drain_window (keep if reset <=30m)   - 429/RESOURCE_EXHAUSTED intercept -> failover|
+---------------------------------------------------------------------------------------+
```

### 2.1 Quota Telemetry: Google Cloud Internal API
Berbeda dengan asumsi awal bahwa kuota di-scrape dari CLI, OpenAG melakukan HTTP request langsung ke Google Cloud Code internal API menggunakan OAuth access token akun:
- **Endpoints**:
  - Primary: `https://daily-cloudcode-pa.googleapis.com`
  - Fallback: `https://cloudcode-pa.googleapis.com`
- **Method & Path**: `POST /v1internal:retrieveUserQuotaSummary`
- **Payload**: `{"project": ""}`
- **Headers**:
  ```http
  Authorization: Bearer <accessToken>
  Content-Type: application/json
  User-Agent: Antigravity/2.5.5
  ```
- **Response Structure**:
  Mengembalikan array `groups` atau `models`. Setiap group berisi `buckets` dengan:
  - `bucketId`: Mengidentifikasi interval jendela (`5h`, `hourly`, `weekly`).
  - `remainingFraction`: Nilai fraksi `0.0` - `1.0` (dikonversi ke persentase `0%` - `100%`).
  - `resetTime`: ISO timestamp waktu reset (misal: `"2026-10-05T09:45:00Z"`).
- **Subscription Tier Detection**:
  Memanggil `POST /v1internal:loadCodeAssist` dengan payload `{"metadata": {"ideType": "ANTIGRAVITY", "ideVersion": "2.5.5"}}` untuk mendeteksi apakah akun bertipe `ultra`, `pro`, `plus`, atau `free`.

### 2.2 Pre-Invocation Lifecycle Hook
Inilah "rahasia" bagaimana OpenAG beralih akun tepat sebelum agent berpikir:
1. Antigravity memiliki sistem Lifecycle Hooks bawaan yang membaca konfigurasi di `~/.gemini/config/hooks.json`.
2. OpenAG mendaftarkan hook:
   ```json
   {
     "openag-auto-rotate": {
       "enabled": true,
       "PreInvocation": [
         {
           "type": "command",
           "command": "node \"<extensionPath>/dist/hook.js\"",
           "timeout": 5
         }
       ]
     }
   }
   ```
3. Sebelum setiap giliran model dimulai, Antigravity memanggil script hook dan menyalurkan metadata lewat `stdin`:
   ```json
   {
     "conversationId": "...",
     "modelName": "gemini-3.7-flash-high",
     "invocationNum": 1,
     "workspacePaths": ["..."]
   }
   ```
4. Hook script mengirim POST ke HookServer internal (`http://127.0.0.1:27182/pre-invocation`).
5. Jika server memutuskan akun harus dirotasi:
   - Token akun baru diaktifkan.
   - Script merespons ke Antigravity via `stdout`:
     ```json
     {
       "injectSteps": [
         {
           "ephemeralMessage": "[OpenAG] Auto-rotated active account to user2@gmail.com (85% remaining for Gemini)"
         }
       ]
     }
     ```
   - Antigravity menerima pesan efemeral tersebut dan langsung menggunakan token baru untuk pemanggilan model berikutnya.

### 2.3 Rotation Decision Engine & Heuristics
Logika pemilihan akun di OpenAG (`TokenManager.autoSelectHighestQuota`) menggunakan algoritma berlapis:
1. **Model Affinity**:
   - Jika akun diberi tag affinity (misal akun A khusus `gemini`, akun B khusus `claude`), sistem menyaring kandidat berdasarkan model yang sedang diminta di prompt.
2. **Reserve Pool Isolation**:
   - Akun bertipe `reserve` tidak akan pernah disentuh selama akun `primary` masih memiliki kuota > 10%.
3. **Tier Prioritization**:
   - Akun berbayar (Ultra / Pro / Plus) dihabiskan terlebih dahulu sebelum menyentuh akun Free Tier.
4. **Predictive Drain Window (Krusial)**:
   - Jika akun aktif saat ini akan mengalami **reset kuota dalam tempo $\le 30$ menit** dan masih memiliki kuota $> 5\%$, sistem **MENOLAK** rotasi. Tujuannya adalah menghabiskan sisa kuota yang akan hangus daripada membuang kuota akun lain.
5. **Imminent Reset Priority**:
   - Jika ada akun standby yang akan me-reset kuota dalam tempo $\le 45$ menit dan kuotanya $> 10\%$, kandidat ini diprioritaskan untuk dihabiskan.
6. **Strategy Modes**:
   - `auto_highest`: Menghitung skor `score = percent + (isImminent ? 1000 : 0) + tierBonus`. Akun dengan skor tertinggi dipilih.
   - `cache_optimized` (*Sticky Cache*): Mempertahankan akun aktif selama kuotanya $> 15\%$ untuk memaksimalkan *prompt cache hit rate* (menghemat biaya dan latensi).
   - `round_robin`: Berpindah melingkar antar akun yang memiliki kuota $> 10\%$.

### 2.4 Reactive Rate-Limit Intercept (429 / RESOURCE_EXHAUSTED)
Selain pemeriksaan preventif sebelum giliran, OpenAG memiliki mekanisme reaktif (`UsageTracker`):
- Memonitor file transcript Antigravity (`~/.gemini/antigravity/brain/<conv-id>/.system_generated/logs/transcript.jsonl`).
- Jika baris transcript berstatus `ERROR` dengan pesan `"RESOURCE_EXHAUSTED"`, `"RATE_LIMIT_EXCEEDED"`, atau `"quota exceeded"`:
  - Seketika menandai kuota akun aktif tersebut menjadi $0\%$.
  - Memicu rotasi failover darurat (`reason: "rate_limit_failover"`).

---

## 3. Local Codebase Comparison & Feasibility

| Fitur / Komponen | `polystree/openag` | `agy-switch` (Kondisi Saat Ini) | Rekomendasi untuk `agy-switch` |
| :--- | :--- | :--- | :--- |
| **Runtime & Bahasa** | TypeScript (VS Code Extension Host) | Python 3.10+ (Standard Library CLI) | Pertahankan Python 3 Stdlib murni. |
| **Metode Cek Kuota** | REST CloudCode API via `fetch()` (Cepat: < 500ms) | Subprocess `agy -p /usage` (Lambat: ~10-30s) | Tambahkan modul direct CloudCode HTTP client berbasis `urllib.request`. |
| **Penyimpanan Kredensial**| VS Code SecretStorage (`context.secrets`) | Vault file `~/.antigravity-vault/` (mode `0600`) | Pertahankan Vault kita (lebih aman & independen). |
| **Mekanisme Switch** | In-memory USS API (`setOAuthTokenInfo`) | Dual-destination atomic write (`os.replace`) | Pertahankan atomic write + language_server restart supervisor. |
| **Pemicu Auto-Rotate** | `PreInvocation` Hook + File Watcher | Belum ada (manual via CLI `agy-switch switch`) | Buat `PreInvocation` Hook native Antigravity + Background Daemon. |
| **Strategi Rotasi** | `auto_highest`, `cache_optimized`, `round_robin` | Sorting statis di `agy-switch quota` | Adopsi algoritma OpenAG ke kelas Python `RotationEngine`. |
| **Turn Safety** | Bergantung pada lifecycle IDE host | Dilindungi Process Supervisor (`/proc` check) | Pertahankan Process Supervisor; hook berjalan di sela giliran (aman). |

### Kendala & Batasan Lingkungan Kita
1. **Zero External Pip Dependencies**:
   - Kita tidak boleh menggunakan `requests`, `aiohttp`, atau `watchdog`. Semua networking harus menggunakan `urllib.request`, threading menggunakan `threading`, dan file monitoring menggunakan loop `os.stat` / inotify sederhana jika dibutuhkan.
2. **Language Server Reload Safety**:
   - OpenAG berjalan *di dalam* IDE sehingga dapat memanggil API internal IDE JS runtime.
   - `agy-switch` berjalan di tingkat OS/CLI. Ketika `agy-switch` mengganti file token di `~/.gemini/`, Antigravity 2.0 / `language_server` memerlukan reload kredensial jika tidak membaca file token di setiap request.
   - Namun, pada Antigravity Lifecycle Hook `PreInvocation`, giliran agent **belum dimulai**, sehingga ini adalah titik paling aman untuk melakukan sinkronisasi file token!

---

## 4. Recommended Architecture for `agy-switch`

Untuk menghadirkan fitur auto-rotate sekelas OpenAG namun dengan keunggulan ketahanan *systems-level* khas `agy-switch`, arsitektur berikut direkomendasikan:

```
                                  +-----------------------+
                                  |   Antigravity Loop    |
                                  +-----------+-----------+
                                              |
                          (Fires before Model Turn Begins)
                                              v
                                  +-----------------------+
                                  | PreInvocation Hook    |
                                  | (hooks.json)          |
                                  +-----------+-----------+
                                              | stdin (JSON: modelName, convId)
                                              v
+-----------------------+         +-----------------------+
| CloudCode Quota API   | <=====> | agy-switch Hook       |
| (urllib.request)      |         | Runner (Python Stdlib)|
| Fast HTTP Telemetry   |         +-----------+-----------+
+-----------------------+                     |
            ^                                 | Evaluates Quota & Strategy
            |                                 v
+-----------+-----------+         +-----------------------+
| Vault Cache           | <=====> | RotationEngine        |
| last_known_quota.json |         | - auto_highest        |
+-----------------------+         | - cache_optimized     |
                                  | - drain_window        |
                                  +-----------+-----------+
                                              |
                                              | If Rotation Needed:
                                              v
                                  +-----------------------+
                                  | AgySwitchEngine       |
                                  | .switch_tokens()      |
                                  | (Atomic os.replace)   |
                                  +-----------+-----------+
                                              |
                                              | stdout (JSON: ephemeralMessage)
                                              v
                                  +-----------------------+
                                  | Model Turn Executes   |
                                  | with New Credentials  |
                                  +-----------------------+
```

### 4.1 Komponen 1: Fast HTTP Quota Client (`QuotaClient`)
Buat class internal di `scripts/agy_switch.py` yang menggunakan `urllib.request`:
- Mengambil `access_token` dari token vault.
- Jika expired, gunakan `refresh_token` untuk request token baru ke `https://oauth2.googleapis.com/token`.
- POST ke `https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary`.
- Parsing bucket `gemini` dan `claude` (5h dan weekly) dalam hitungan < 300ms tanpa spawn proses `agy`.
- Simpan hasilnya ke `~/.antigravity-vault/cache/last_known_quota.json`.

### 4.2 Komponen 2: Rotation Engine (`RotationEngine`)
Porting algoritma OpenAG dengan aturan:
- **Input**: Model target (dari payload hook), daftar akun aktif, dan snapshot kuota.
- **Rules**:
  1. *Drain Window*: Jika akun saat ini reset dalam $\le 30$ menit dan kuota $> 5\%$, pertahankan.
  2. *Imminent Reset*: Cari akun lain yang reset dalam $\le 45$ menit dan kuota $> 10\%$.
  3. *Cache Optimized*: Jika kuota akun saat ini $> 15\%$, pertahankan (hemat cache).
  4. *Auto Highest*: Pilih akun dengan sisa kuota terbesar.
- **Output**: Nama akun target atau `None` jika tidak perlu berpindah.

### 4.3 Komponen 3: Antigravity Lifecycle Hook Handler
Daftarkan hook di `.agents/hooks.json` atau `~/.gemini/config/hooks.json`:
```json
{
  "agy-switch-auto-rotate": {
    "enabled": true,
    "PreInvocation": [
      {
        "type": "command",
        "command": "python3 /srv/agy-workspaces/booster/agy-switcher/scripts/agy_switch.py hook-pre-invocation",
        "timeout": 5
      }
    ]
  }
}
```
Ketika dieksekusi:
1. Membaca `stdin` JSON (`modelName`, dll).
2. Membaca cache kuota (atau melakukan fast probe jika cache berumur > 60 detik).
3. Jika akun perlu dirotasi:
   - Jalankan `engine.switch_tokens(target_account, restart_server=False)`.
     *(Catatan: `restart_server=False` saat pre-invocation hook karena penggantian token terjadi sebelum turn dijalankan).*
   - Kirim `ephemeralMessage` ke `stdout` agar user mendapatkan notifikasi visual di chat:
     ```json
     {
       "injectSteps": [
         {
           "ephemeralMessage": "[agy-switch] Auto-rotated active account to 'account-b' (82% Gemini quota remaining)"
         }
       ]
     }
     ```
4. Jika tidak perlu rotasi, output `{}`.

### 4.4 Komponen 4: Background Daemon & Failover Watcher (`agy-switch daemon`)
Menyediakan perintah CLI opsional `agy-switch daemon`:
- Berjalan di background (misal via systemd user service atau screen/tmux).
- Memperbarui kuota akun setiap $N$ menit menggunakan fast CloudCode API.
- Memantau baris akhir transcript percakapan aktif untuk mendeteksi error `RESOURCE_EXHAUSTED` (429) dan seketika melakukan emergency failover.

---

## 5. Design Decisions & Handoff to `/to-spec`

Berikut adalah *Decision Frontier* (pilihan desain arsitektural) yang perlu disepakati sebelum tahap implementasi spesifikasi:

### Fork 1: Metode Pengambilan Kuota (Fast HTTP vs CLI Subprocess)
- **Opsi A (Fast Direct CloudCode HTTP - Direkomendasikan)**: Menggunakan `urllib.request` langsung ke Google Cloud Code PA internal API. Latensi < 300ms, akurat, hemat resource.
- **Opsi B (CLI Fallback)**: Tetap memakai `agy -p /usage` tetapi hanya saat probe manual.

### Fork 2: Arsitektur Eksekusi Auto-Rotate
- **Opsi A (Hook Native Antigravity Saja - Sangat Ringan)**: Cukup daftarkan `PreInvocation` hook di `hooks.json`. Tanpa daemon background konstan, zero-memory footprint.
- **Opsi B (Hook + Background Daemon Hybrid - Paling Lengkap)**: Hook menangani rotasi sebelum turn; Daemon menangani background refresh kuota dan deteksi failover 429 reaktif.

### Fork 3: Strategi Default Rotasi
- **Opsi A (`auto_highest`)**: Selalu prioritaskan akun dengan kuota tertinggi.
- **Opsi B (`cache_optimized` - Rekomendasi OpenAG)**: Tetap di akun saat ini selama kuota $> 15\%$ agar prompt cache tidak hilang, baru berpindah jika kuota menipis.

---

### Recommended Next Steps
1. Jalankan **/grill-me** atau **/grill-with-docs** untuk memvalidasi pilihan desain di atas bersama user.
2. Lanjutkan ke **/to-spec** untuk menyusun spesifikasi teknis dan test case TDD.

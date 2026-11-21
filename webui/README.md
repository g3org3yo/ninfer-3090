# NInfer Studio

Δικό μας web UI για το `ninfer-serve` — chat + ζωντανά metrics, χωρίς εξωτερικές εξαρτήσεις
(μόνο Python standard library).

```
start_studio.bat      # ξεκινά engine (:8082) + UI (:8090) και ανοίγει browser
stop_studio.bat       # κλείνει και τα δύο
webui\server.py       # proxy + parser των Prometheus metrics
webui\index.html      # UI
webui\app.js          # streaming chat (SSE), telemetry, vision
```

## Τι κάνει

- **Chat με streaming** πάνω στο `/v1/chat/completions`, με `reasoning_content` (thinking) σε
  ξεχωριστό, dim block και `reasoning_effort: none|low|medium|high` από το topbar.
- **Εικόνες (vision)**: κουμπί 🖼 ή drag & drop. Ο server τρέχει με `--vision`, άρα δέχεται
  `image_url` (data URI). Το artifact `qwen3_8_27b.ninfer` έχει vision tower.
- **Live metrics** από `/metrics` + `/v1/load`: decode tok/s (sparkline), tok/round, generated,
  prefill tok/s, VRAM, watts, θερμοκρασία, GPU util, KV pages, context usage, speculative backend.
- **Κανένα key, κανένα cloud**: ο proxy μιλά μόνο σε `127.0.0.1:8082`. Αν βάλεις `--api-key` στον
  engine, βάλε `set NINFER_API_KEY=...` πριν το `start_studio.bat` (δεν γράφεται σε config).

## Ρυθμίσεις engine (start_studio.bat)

| flag | γιατί |
|---|---|
| `--spec mtp --draft-tokens 3` | καλύτερο acceptance σε πραγματικό κείμενο (61%) |
| `--prefill-cublas --prefill-chunk 4096` | prefill ~+67% |
| `--max-context 32768 --kv-capacity auto` | 512 pages KV, 3.35 GiB runtime |
| `--vision` | image input· +0.84 GiB tower, χωράει στα 24 GB |
| `--log-stats-interval-ms 5000` | stats κάθε 5s στο console |

Μετρήσεις στην 3090 (24 prompts × 512 tokens, rk4v4, ctx 8192):
`plain 34.0` · `mtp K=3 101.6 (61.4% accept)` · `dflash2 K=7 105.3 (36.6% accept)` tok/s decode.
Με vision request: prefill ~1.500 tok/s, decode ~101 tok/s.

### Swift 1.5 W4A16 (default πλέον)

`models\Swift-1.5-Qwen3.8-27b.ninfer` — 20,4 GB, Apache-2.0 base, δωρεάν έως $1M τζίρο.

| | qwen3_8_27b (rk4v4) | Swift 1.5 W4A16 |
|---|---|---|
| weights | 17,7 GiB | **16,9 GiB** (με vision tower) |
| decode (8 real prompts) | 109,6 tok/s | 107,5 tok/s |
| tok/round @mtp K=3 | 2,85 | **2,90** |
| end-to-end | 100,9 tok/s | 98,1 tok/s |
| vision | ✅ | ✅ (`--vision`, +0,2 GiB) |
| thinking | verbose | πιο σύντομο (58,5% λιγότερα κατά τον vendor) |

Πρακτικά ίδια ταχύτητα, ~0,8 GiB λιγότερη VRAM, λιγότερα thinking tokens → Swift είναι το default,
με αυτόματο fallback στο `qwen3_8_27b.ninfer` αν λείπει το αρχείο.

## Env

```
NINFER_URL       default http://127.0.0.1:8082
NINFER_API_KEY   προαιρετικό bearer
NINFER_UI_PORT   default 8090
```

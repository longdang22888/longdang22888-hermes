# jev-skill-suggest

Gợi ý tối đa **một** Hermes skill mỗi turn + một routing hint, dùng TypeSafe Jev.
Chạy ở hook `pre_llm_call`; kết quả được bơm thành một block cache-safe vào đầu user
message (không phá system prompt cache).

## Cơ chế (theo cookbook skill-suggestion của TypeSafe)

Hai request TypeSafe mỗi turn (pre-side):

1. **Call 1** — fan-out trên cùng một `state` (1 request):
   - `which_skill` (choice): rank toàn bộ roster skill + nhãn `none`.
   - 3 noul gate: `act_on_stuff`, `follow_steps`, `just_talk` — quyết định turn có cần skill không.
   - 3 câu routing: `needs_code` (noul), `difficulty` (score 0–4), `delegatable` (noul).
2. **Call 2** — đọc lại top-3 (đầy đủ description + excerpt) và chọn skill thật, tự do chọn `none`.

Jev chỉ trả lời; mọi ngưỡng nằm trong code ở `jev_suggest.py`.

## Ngưỡng (hằng số đầu file)

| Hằng | Giá trị | Ý nghĩa |
|---|---|---|
| `SHORTLIST` | 3 | số ứng viên đưa vào call 2 |
| `EXCERPT_CHARS` | 700 | độ dài excerpt mỗi skill |
| `GATE_THRESHOLD` | 0.30 | gate_mean dưới mức này → bỏ qua skill |
| `FITS_THRESHOLD` | 0.30 | best-fit dưới mức này → không gợi ý |
| `DELEGATE_WHOLE_MAX` | 2 | difficulty ≤ medium → hint giao toàn bộ |
| `DELEGATE_PART_MIN` | 3 | difficulty ≥ hard → hint tách subtask |

## Routing hint

Chỉ là **advisory** — không tự đổi model. Khi `delegatable=true`:
- `difficulty ≤ medium` → hint giao toàn bộ cho model nhẹ (agy).
- `difficulty ≥ hard` → hint cân nhắc tách phần giao subagent agy song song.
- `needs_code=false` → caller có thể bỏ qua hint (gate `pre_verify` không fire nếu không sửa file).

## Files

| File | Vai trò |
|---|---|
| `jev_suggest.py` | pipeline 2-call, `suggest()`, `load_roster()`, `_delegate_hint()` |
| `__init__.py` | hook `pre_llm_call` → `suggest()` → nối `suggestion_block` + `routing_block` |
| `plugin.yaml` | manifest (`provides_hooks: [pre_llm_call]`, `requires_env: [TYPESAFE_API_KEY]`) |

## Lưu ý

- **Stdlib-only** (`urllib`/`json`/`os`/`re`/`pathlib`) — plugin chạy process riêng.
- `TYPESAFE_API_KEY` đọc từ env hoặc `~/.hermes/.env`; thiếu → `suggest()` trả rỗng, turn vẫn chạy (fail-open).
- Roster skill đọc từ `~/.hermes/skills/**/SKILL.md`.

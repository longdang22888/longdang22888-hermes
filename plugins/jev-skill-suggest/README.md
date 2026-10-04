# jev-skill-suggest

Gợi ý các Hermes skill phù hợp nhất mỗi turn (rank theo score) + một routing hint
(toolset / MCP / delegate), dùng TypeSafe Jev. Chạy ở hook `pre_llm_call`; kết quả
được bơm thành một block cache-safe vào đầu user message (không phá system prompt cache).

## Cơ chế

**Hai request TypeSafe mỗi turn:**

1. **Call 1 — fan-out** (1 request, các câu song song chia sẻ cùng `state`):
   - `which_skill` (choice): rank toàn bộ roster skill + nhãn `none`. Các `probabilities`
     chỉ dùng để **chọn shortlist** top-`TOP_N` (softmax, dồn về top-1 — không làm score cuối).
   - 3 noul gate: `act_on_stuff`, `follow_steps`, `just_talk` — quyết định turn có cần skill không.
   - Câu routing: `needs_code` (noul), `difficulty` (score 0–4), `which_toolset` (choice),
     `delegate_plan` (choice 4 nhánh), và `which_mcp` (choice — chỉ khi có MCP cấu hình).

2. **Call 2 — re-rank shortlist** (1 request): mỗi candidate được đọc đầy đủ
   `description` + `excerpt` rồi chấm một **Score** với 5 mức criteria giống hệt nhau
   (comparable). Score thô (0–4) được chuẩn hóa về 0–1 bằng `len(legend)-1`, rồi sort
   giảm dần và lọc theo `FITS_THRESHOLD`.

Jev chỉ trả lời; mọi ngưỡng nằm trong code ở `jev_suggest.py`.

## Ngưỡng (hằng số đầu file)

| Hằng | Giá trị | Ý nghĩa |
|---|---|---|
| `TOP_N` | 5 | số skill đưa vào shortlist (call 2) và tối đa trong gợi ý |
| `EXCERPT_CHARS` | 700 | độ dài excerpt mỗi skill đưa vào call 2 |
| `GATE_THRESHOLD` | 0.30 | gate_mean dưới mức này → bỏ qua skill |
| `FITS_THRESHOLD` | 0.30 | score call-2 (0–1) dưới mức này → không gợi ý |

## Routing hint

Chỉ là **advisory** — không tự đổi model, không tự spawn subagent. Các trường:

- `needs_code` (y/n) — turn có cần sửa code/file không.
- `difficulty` — `trivial` / `easy` / `medium` / `hard` / `very_hard`.
- `toolset` — một trong `TOOLSET_INDEX` (web, terminal, file, code_execution, skills,
  memory, delegation, browser, cronjob, clarify, vision, tts, todo, session_search) hoặc `none`.
- `mcp` — một trong các MCP server đang cấu hình (đọc từ `config.yaml → mcp_servers`),
  hoặc `none`; chỉ được hỏi khi có server.
- `delegate_plan` — `no_delegate` / `delegate_whole` / `delegate_parallel` / `delegate_single`,
  ánh xạ sang một directive cụ thể hướng dẫn dùng `delegate_task`.

## Files

| File | Vai trò |
|---|---|
| `jev_suggest.py` | pipeline 2-call, `suggest()`, `load_roster()`, `load_mcp_servers()`, `_score_fit()` |
| `__init__.py` | hook `pre_llm_call` → `suggest()` → nối `relevance_block` + `routing_block` |
| `plugin.yaml` | manifest (`provides_hooks: [pre_llm_call]`, `requires_env: [TYPESAFE_API_KEY]`) |
| `test_jev_suggest.py` | pure-logic test (routing + `load_mcp_servers` + `_score_fit` + suggestion block), không gọi API |

## Lưu ý

- **Stdlib-only** (`urllib`/`json`/`os`/`re`/`pathlib`) — plugin chạy process riêng.
- `TYPESAFE_API_KEY` đọc từ env hoặc `~/.hermes/.env`; thiếu → `suggest()` trả rỗng, turn vẫn chạy (fail-open).
- Roster skill đọc từ `~/.hermes/skills/**/SKILL.md`.
- MCP server đọc từ `config.yaml` bằng hàm scan YAML-lite (`load_mcp_servers`), không import `yaml`.

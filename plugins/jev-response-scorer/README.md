# jev-response-scorer

Chấm điểm mỗi câu trả lời đã hoàn thành của Hermes bằng Jev trên **2 câu hỏi**, rồi
suy ra quyết định stop/continue. Là **observer** — chạy ở hook `post_llm_call`, return
bị bỏ qua nên không bao giờ chặn hay đổi câu trả lời. Dữ liệu ghi vào ledger JSONL làm
RL label để huấn luyện/phân tích sau.

## Hai câu hỏi (trong `_questions()`)

| Tên | Kiểu | Ý nghĩa |
|---|---|---|
| `satisfied_intent` | noul | Đã thỏa mãn intent user chưa? |
| `needs_research` | noul | Có cần research tiếp (tavily/exa/parallel) không? |

## Quy tắc quyết định (`decide()`, ngưỡng trong code)

| Ngưỡng | Giá trị |
|---|---|
| `SATISFIED_MIN` | 0.50 |
| `NEEDS_RESEARCH_MIN` | 0.50 |

- `satisfied_intent >= 0.50` → **stop** (đã thỏa mãn thì không research thêm).
- ngược lại, `needs_research >= 0.50` → **continue_research**.
- ngược lại → **continue_other**.

## Ledger

- Ghi vào `~/.hermes/logs/jev_rewards.jsonl`, mỗi turn một dòng JSON: `ts`, `session_id`,
  `user_message`, `assistant_response`, `satisfied_intent`, `needs_research`, `decision`, `model`.
- **Rotation**: active file > `LEDGER_MAX_BYTES` (5 MB) → đổi tên kèm timestamp, giữ tối đa
  `LEDGER_KEEP_ROTATED` (3) file cũ.

## Steering (cache-safe)

`__init__.py` đăng ký một system-prompt section (`jev-response-scorer.criteria`) qua
`register_system_prompt_section`, position `after_memory`, render **một lần mỗi session**
nên không phá prompt cache. Nội dung mô tả 2 tiêu chí chấm để agent biết cách bị đánh giá.

## Files

| File | Vai trò |
|---|---|
| `jev_scorer.py` | `_questions()`, `decide()`, `score_response()`, `record_turn()`, rotation |
| `__init__.py` | hook `post_llm_call` → `record_turn()` + system-prompt section |
| `plugin.yaml` | manifest (`provides_hooks: [post_llm_call]`) |

## Lưu ý

- **Stdlib-only** (`urllib`/`json`/`os`/`pathlib`/`datetime`).
- `post_llm_call` fire **sau** khi turn xong → không ép agent tiếp tục (đó là việc của
  `jev-verify-gate`).
- Lỗi scoring là best-effort — không làm hỏng turn.

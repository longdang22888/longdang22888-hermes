# jev-verify-gate

Control gate cho **coding turn**: ở hook `pre_verify`, hỏi Jev một fan-out request và ép
agent tiếp tục khi change chưa đạt yêu cầu. Đây là control hook duy nhất khả dụng cho QC
code — `post_llm_call` chỉ là observer (return bị bỏ qua), còn `pre_verify` trả
`{"action": "continue", "message": nudge}` để re-enter loop trước khi turn finalize.

## Điều kiện fire

`pre_verify` chỉ fire khi agent **đã sửa file** và chuẩn bị finish
(`_turn_file_mutation_paths` khác rỗng). Do đó gate này **coding-only** — không ép được
task Q&A thuần. Loop bị chặn bởi `agent.max_verify_nudges` (default 3).

## Câu hỏi (static policy trong `QUESTIONS`)

| Tên | Kiểu | Hỏi gì |
|---|---|---|
| `requirements_met` | noul | Đã thỏa mãn đầy đủ yêu cầu task chưa? |
| `scope_creep` | noul | Có đụng file/hành vi ngoài phạm vi không? |
| `regression_risk` | score 0–4 | Nguy cơ regression (chuẩn hoá về 0..1 theo legend) |
| `suspicious_change` | noul | Diff có code đáng ngờ không? |
| `needs_review` | noul | Có cần review sâu hơn không? |
| `missing` | choice | Còn thiếu cụ thể cái gì? (none/tests/core_functionality/error_handling/documentation/other) |

## Ngưỡng (`decide()` — code owns policy)

| Hằng | Giá trị | Kích hoạt |
|---|---|---|
| `REQUIREMENTS_MET_MIN` | 0.80 | dưới → nudge theo `missing` |
| `SCOPE_CREEP_MAX` | 0.70 | trên → nudge hoàn tác |
| `REGRESSION_RISK_MAX` | 0.70 | trên → nudge chạy test |
| `SUSPICIOUS_MAX` | 0.70 | trên → nudge rà soát diff |
| `NEEDS_REVIEW_MAX` | 0.65 | trên → nudge review lại |

`missing` map sang nudge cụ thể bằng `_missing_nudge()` (VD `tests` → *"Thiếu unit test..."*);
`missing=none` → lệnh tự-audit (liệt kê từng requirement, đánh dấu xong/chưa).

## Gom state

Payload `pre_verify` thật chỉ có `session_id/platform/model/coding/attempt/final_response/
changed_paths` — **không có** task/git_diff/tests. Plugin tự gom qua các hook khác:

- `pre_llm_call` → lưu `user_message` mới nhất làm `task`.
- `post_tool_call` → giữ `MAX_EVIDENCE` (20) tool call gần nhất, mỗi result/args cắt
  `MAX_RESULT_CHARS`/`MAX_ARGS_CHARS` (400), args chứa key/token/password bị `[REDACTED]`.
- `on_session_end` → dọn state.

## Fail-open

Mọi lỗi (thiếu API key, HTTP error, timeout, exception) → `evaluate()` trả `None` → finish
bình thường. Không bao giờ kẹt turn.

## Files

| File | Vai trò |
|---|---|
| `jev_verify.py` | `QUESTIONS`, `decide()`, `_missing_nudge()`, `evaluate()`, state hooks |
| `__init__.py` | hook `pre_verify` → `evaluate()` → return `{"action":"continue"}` hoặc `None` |
| `plugin.yaml` | manifest (`provides_hooks: [pre_llm_call, post_tool_call, pre_verify, on_session_end]`) |

## Test

`~/.hermes/cache/scratch/jev-verify-gate-tests/test_jev_verify.py` — 13 test
(pure-logic + monkeypatch), chạy bằng `python3 -m pytest test_jev_verify.py -q`.

## Lưu ý

- **Stdlib-only** (`urllib`/`json`/`os`/`pathlib`).
- Nudge message bằng tiếng Việt, cùng ngôn ngữ task.
- Log accept/continue ở mức `DEBUG` (không làm ồn `agent.log`).

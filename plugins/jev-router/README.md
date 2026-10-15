# jev-router

Turn-router advisory qua TypeSafe Jev: mỗi turn gọi **đúng một** request tới
`https://api.typesafe.ai/v1/systemone` (model `jev-latest`) với 10 câu hỏi độc lập,
rồi bơm một **routing hint thuần advisory** vào user message qua hook `pre_llm_call`.
Fail-open toàn phần, stdlib-only, không commit/override quyết định nào của user.

## Câu hỏi (1 request — fan-out, câu độc lập chia sẻ state)

| Tên | Kiểu | Hỏi gì |
|---|---|---|
| `difficulty` | score 0–4 | Độ khó (5 mức concrete: trivial/easy/medium/hard/very_hard) |
| `needs_execution` | noul | Có cần thực thi (chạy lệnh, sửa file, gọi tool) không? |
| `missing_critical_context` | noul | Có thiếu context critical (file, credential, ràng buộc) không? |
| `coding_scope` | choice | non_coding / inspect_or_review / implement_or_fix / mixed |
| `delegation_value` | noul | Delegate có giá trị hơn làm inline không? |
| `independent_workstream` | noul | Tách được thành workstream độc lập không? |
| `parallel_edit_risk` | noul | Chạy song song có nguy cơ conflict edit không? |
| `needs_deep_iteration` | noul | Có cần lặp build/test/fix hoặc research sâu không? |
| `completion_evidence` | choice | not_started / in_progress / blocked / claimed_but_unverified / verified_complete |
| `delegate_target` | choice | none / subagent / agy / claude_code (CLI không cài bị suppress) |

`delegate_target` chỉ offer `agy` / `claude_code` khi `shutil.which()` thấy CLI tương ứng
(`agy`, `claude`); mặc định luôn có `none` + `subagent`. Choice guard: giá trị lạ hoặc
target không khả dụng → `none`.

## Hook `pre_llm_call` — payload thật (trung thực về contract)

Runtime gửi: `session_id`, `task_id`, `turn_id`, `user_message`, `conversation_history`,
`is_first_turn`, `model`, `platform`, `parent_session_id`, `sender_id`
(xem `agent/turn_context.py::_collect_pre_llm_call_context`). Plugin **chỉ dùng**
`user_message` — `conversation_history`, tool output, source code **không bao giờ** được
gửi đi. Trả về str context (được nối vào user message); lỗi → `""`.

## State, redaction, limit, timeout

- State gửi đi chỉ có `{"user_message": ...}` — bounded `MAX_MESSAGE_CHARS` (1200,
  giữ head 400 + tail 700 khi dài quá).
- Redaction 2 lớp: pattern secret (ghp_/sk-/xox/AKIA/AIza/Bearer/`*KEY=*`) + giá trị
  secret đã biết từ env file (`KEY|TOKEN|SECRET|PASSWORD|PASS`, ≥12 ký tự) → `[REDACTED]`
  trước khi rời máy.
- `REQUEST_TIMEOUT_S = 6` — hết timeout thì fail-open ngay, không giữ turn.

## API key

Đọc `TYPESAFE_API_KEY` theo thứ tự: env → `HERMES_HOME/.env` (profile đang active) →
`~/.config/typesafe/env`. Parse an toàn theo dòng `KEY=value` (không shell, không eval).
Fallback runtime này được ủy quyền cho task hiện tại; key chỉ nằm ở header
`Authorization`, **không bao giờ** xuất hiện trong log/metrics/output.

## Advisory-only — preserve user directive & approvals

Block bơm vào mở đầu bằng: *"advisory only: the user's explicit directive and any
required approvals always take precedence"*. Khi `target != none` có thêm:
*"Delegation is one level only: a delegate must never delegate further"* — không
recursive delegation. Mọi phán đoán của Jev chỉ là hint; ngưỡng & policy nằm trong code.

## Fail-open

Mọi lỗi — thiếu key, endpoint không phải HTTPS, DNS/timeout, HTTP 4xx/5xx, JSON hỏng,
kết quả thiếu `answers` — → không inject gì, turn chạy bình thường. Câu trả lời sai
shape được guard về default an toàn (choice lạ → default, score ngoài khoảng → clamp 0–4).

## Logging

`logs/jev_metrics.jsonl` (rotate 2MB) chỉ ghi metadata + kết quả: latency, ok/error,
verdict fields, injected_chars. **Không** ghi request text, không ghi key.

## Files

| File | Vai trò |
|---|---|
| `jev_router.py` | `QUESTIONS`/`build_questions()`, `parse_answers()` (choice guards), `route()`, `advisory_block()`, key/redaction/metrics |
| `__init__.py` | hook `pre_llm_call` → `route()` → str advisory block hoặc `""` (fail-open) |
| `plugin.yaml` | manifest (`provides_hooks: [pre_llm_call]`, `requires_env: [TYPESAFE_API_KEY]`) |
| `test_jev_router.py` | unittest + mock: schema, choice guards, availability, truncation/redaction, contract, invalid API, fail-open, hook registration |

## Test

```bash
cd plugins/jev-router && python3 -m unittest test_jev_router -v
```

43 test, không network thật, không cần API key.

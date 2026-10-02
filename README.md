# longdang22888-hermes

Bộ plugins + skills cá nhân của longdang cho [Hermes Agent](https://hermes-agent.nousresearch.com).

## Nội dung

| Thư mục | Gì | Cài bằng |
|---|---|---|
| `plugins/jev-skill-suggest` | Gợi ý ≤1 skill + routing hint (delegate) qua TypeSafe Jev, hook `pre_llm_call` | `hermes plugins install longdang22888/longdang22888-hermes/plugins/jev-skill-suggest` |
| `plugins/jev-response-scorer` | Chấm điểm response (2 câu satisfied/needs_research), observer qua `post_llm_call`, ghi RL label | `hermes plugins install longdang22888/longdang22888-hermes/plugins/jev-response-scorer` |
| `plugins/jev-verify-gate` | Control gate QC coding qua `pre_verify` — ép agent làm tiếp khi change chưa đạt | `hermes plugins install longdang22888/longdang22888-hermes/plugins/jev-verify-gate` |
| `skills/brainstorming` | Skill `/brainstorming` — đưa 3+ phương án A/B/C kèm đề xuất | copy thủ công vào `~/.hermes/skills/productivity/` |

## Nguyên tắc chung

- **Stdlib-only** (`urllib`/`json`/`os`/`pathlib`) — plugin chạy trong process riêng của Hermes, không import `httpx`/`typesafe-sdk`/`hermes_cli`.
- **Fail-open** — Jev lỗi/timeout → turn vẫn chạy bình thường.
- **Threshold nằm trong code** — Jev chỉ trả phán đoán (noul/score/choice), mọi ngưỡng & hành động do plugin quyết định.
- **`TYPESAFE_API_KEY`** đọc từ env hoặc `~/.hermes/.env`; mỗi plugin khai báo `requires_env: [TYPESAFE_API_KEY]` để `hermes plugins install` nhắc nhập khi thiếu.

## Yêu cầu

- Hermes Agent ≥ v0.21 (hook `pre_verify`, plugin manifest top-level `provides_hooks`).
- API key TypeSafe (`TYPESAFE_API_KEY`).

## Cài đặt

```bash
# từng plugin một (khuyên dùng — kiểm soát được)
hermes plugins install longdang22888/longdang22888-hermes/plugins/jev-skill-suggest
hermes plugins install longdang22888/longdang22888-hermes/plugins/jev-response-scorer
hermes plugins install longdang22888/longdang22888-hermes/plugins/jev-verify-gate
```

Mỗi plugin có `README.md` riêng mô tả hook, câu hỏi, ngưỡng, fail-open.

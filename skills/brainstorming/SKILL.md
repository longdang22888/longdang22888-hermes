---
name: brainstorming
description: "Brainstorm: đưa 3+ phương án A/B/C kèm đề xuất và lý do."
version: 0.1.0
author: longdang (longdang), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [brainstorm, decision, options, ideation]
---

# Brainstorming Skill

Giúp người dùng khám phá các hướng đi trước khi hành động. Skill này **không** tự
thực thi giải pháp — nó phân tích vấn đề rồi đưa ra các phương án để người dùng chọn.
Chạy bằng `/brainstorming <vấn đề>` hoặc khi người dùng mô tả một quyết định cần cân nhắc.

## When to Use

- Người dùng cần quyết định giữa nhiều hướng đi mà chưa rõ nên chọn gì.
- Một vấn đề mở ("tôi nên…?", "có cách nào để…?") cần khám phá phương án.
- Cần so sánh đánh đổi (trade-off) trước khi cam kết một giải pháp.

Don't use for: yêu cầu thực thi trực tiếp ("hãy làm X") — khi đó chỉ làm X, không mở rộng.

## Prerequisites

Không cần API key hay công cụ đặc biệt. Dùng các tool có sẵn nếu cần thêm ngữ cảnh:
`read_file`, `search_files`, `web_search`, `web_extract`. Chỉ thu thập ngữ cảnh khi
câu hỏi của người dùng chưa đủ thông tin; không đi lạc sang nghiên cứu sâu nếu không cần.

## How to Run

```
/brainstorming Tôi nên triển khai tính năng X bằng cách nào?
/brainstorming Có nên migrate DB sang Postgres không?
```

## Procedure

1. **Hiểu intent.** Tóm tắt 1 câu mục tiêu thực sự của người dùng — không phải câu hỏi
   theo nghĩa đen, mà là điều họ muốn đạt được. Đặt câu hỏi làm rõ nếu intent mơ hồ.
   *Hoàn thành khi:* viết được một câu "người dùng muốn <kết quả>, bị chặn bởi <trở ngại>".

2. **Thu ngữ cảnh và môi trường.** Xác định các ràng buộc đang có: môi trường hiện tại
   (cwd, OS, stack, dữ liệu sẵn có), hạn chế (thời gian, ngân sách, kỹ năng), và ưu tiên
   người dùng đã ngầm nói. Dùng `read_file`/`search_files` nếu cần soi codebase hiện tại.
   *Hoàn thành khi:* liệt kê được danh sách ràng buộc ảnh hưởng trực tiếp đến lựa chọn.

3. **Đưa ra ít nhất 3 phương án A, B, C (nhiều hơn nếu tự nhiên).** Mỗi phương án phải:
   - đứng riêng được (một hướng đi rõ, không phải biến thể nhỏ của nhau);
   - ghi 1-2 câu nói rõ nó là gì;
   - kèm ưu điểm và đánh đổi chính.
   *Hoàn thành khi:* có ≥3 phương án, mỗi phương án mô tả được cái được và cái mất.

4. **Đề xuất chọn 1 phương án.** Nêu rõ chọn phương án nào và **diễn giải lý do** dựa trên
   ngữ cảnh và ràng buộc đã thu ở bước 2 — không phải ý thích chủ quan. Nếu hai phương án
   ngang nhau, nói thẳng là ngang và nêu điều kiện quyết định còn thiếu.
   *Hoàn thành khi:* có một đề xuất rõ ràng kèm lý do gắn với ràng buộc thực tế.

5. **Hỏi bước tiếp theo.** Kết thúc bằng câu hỏi cho người dùng chọn hướng đi, không tự
   chạy tiếp trừ khi được yêu cầu.

## Format đầu ra

```
Mục tiêu: <1 câu>
Ràng buộc: <gạch đầu dòng, ngắn>

A. <tên> — <mô tả ngắn>
   + <ưu điểm>   − <đánh đổi>
B. <tên> — <mô tả ngắn>
   + <ưu điểm>   − <đánh đổi>
C. <tên> — <mô tả ngắn>
   + <ưu điểm>   − <đánh đổi>

Đề xuất: <A/B/C> — <lý do gắn với ràng buộc>

Bạn muốn đi theo hướng nào?
```

## Pitfalls

- **Đừng biến một phương án thành 3 biến thể giả.** A/B/C phải khác nhau về bản chất.
- **Đừng đề xuất mà không có lý do.** Lý do phải trích được từ ngữ cảnh, không phải "thấy hợp lý".
- **Đừng tự thực thi sau khi đề xuất.** Skill này dừng ở lựa chọn; hành động là bước sau khi người dùng chọn.
- **Đừng thu thập ngữ cảnh thừa.** Nếu câu hỏi đã đủ thông tin, đừng mở `web_search` vô ích.

## Verification

- Có ≥3 phương án phân biệt rõ ràng, mỗi phương án có ưu điểm và đánh đổi.
- Có đúng 1 đề xuất kèm lý do gắn với ràng buộc đã nêu.
- Kết thúc bằng câu hỏi chọn hướng đi, chưa thực thi gì.

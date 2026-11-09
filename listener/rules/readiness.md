<!-- rule-file: readiness · version 8 · status ACTIVE · audience: code -->
# "Đủ rõ" — bảng quyết định (TG-08)

Owner 2026-11-05: “Duyệt lọc trước” — nhờ review/duyệt/merge PR hoặc xem issue GitHub đã có link không phải task, bug hay feedback, không log ticket.

Owner 2026-11-06: “Có, là CHAT” — giao việc hoặc nhờ/hỏi đồng nghiệp trong team (tag `@tên`) không phải ticket.

Owner 2026-11-06: “Duyệt F3” — tin chỉ có link, không kèm chữ, là chia sẻ, không phải ticket hay câu hỏi.

Owner 2026-11-06: “Duyệt cả ba” — tin chỉ có từ xã giao (`chat_words`) là CHAT (luật `F4`); kèm bước chuẩn hóa N6 và cụm `work` chung chung trong `facts.md`.

Owner 2026-11-09 (live #118, #119 trong group DEV - NEXORA): “cập nhật thêm context đùa giỡn dựa trên bối cảnh chung trên group để tránh log ticket sai” — tin đùa giữa lúc cả nhóm đang cười không thành ticket: luật `F5` (cục bộ, tin tự có dấu cười) và hướng dẫn model đọc `read_only_context` trong `facts.md`. Owner 2026-11-09: “Duyệt F5” — duyệt luật `F5` với ngưỡng 2 tin / 2 người / 15 phút / 8 tin và danh sách dấu hiện tại, kèm các đánh đổi ghi trong Business DOCS TG-09.

Input: dữ kiện code tính + các trường CÓ theo `facts.md`. Output: một mã trong `states.md`. **Model không thấy file này.**

Cách đọc mọi bảng ở đây:

- Đọc từ trên xuống, **dòng đầu tiên khớp mọi cột là kết quả**.
- Ô `*` = bất kỳ. Ô có một hoặc nhiều giá trị trong backtick = giá trị phải là **một trong** các giá trị đó.
- `cần CÓ tất cả`: mọi trường liệt kê phải CÓ. `cần CÓ ít nhất một`: ít nhất một trường CÓ. Ô `—` = không yêu cầu.

## Bảng: prefilter

Lọc local **trước khi gọi model** và trước bảng `decision`. Chỉ xét tin mới: không phải Reply, không command, không ảnh, không tài liệu. Khớp ⇒ `CHAT` ngay với luật `F1`, không gọi model. Không khớp ⇒ đi tiếp như cũ. Nhóm nhiều tin chỉ thành `CHAT` khi **mọi** tin đều khớp.

| # | Bước |
|---|---|
| P1 | Tin có ít nhất một link GitHub tới PR hoặc issue: `github.com/<owner>/<repo>/pull/<số>` hoặc `github.com/<owner>/<repo>/issues/<số>` |
| P2 | Bỏ ký tự vô hình; bỏ link GitHub ở P1 (đuôi link dừng ở `,` `;`, khoảng trắng và chữ có dấu); còn link khác ⇒ không khớp; bỏ mọi `@tên`, mọi cụm chỉ gồm số hoặc `#` + số; đổi dấu câu, emoji và `_` thành khoảng trắng; chữ thường; tách theo khoảng trắng |
| P3 | Mọi từ còn lại thuộc bảng `prefilter_words`, và có ít nhất một từ loại `việc` |
| P4 | Khớp P1–P3 ⇒ `CHAT` (luật `F1`); báo lỗi sản phẩm hay có chữ ngoài bảng ⇒ không khớp |

## Bảng: prefilter_words

`việc` = việc được nhờ (phải có ít nhất một). `đệm` = xưng hô, xã giao, tên gọi PR/issue.

| Loại | Từ |
|---|---|
| `việc` | `review` |
| `việc` | `rv` |
| `việc` | `duyệt` |
| `việc` | `approve` |
| `việc` | `merge` |
| `việc` | `xem` |
| `việc` | `coi` |
| `việc` | `check` |
| `đệm` | `anh` |
| `đệm` | `em` |
| `đệm` | `chị` |
| `đệm` | `bạn` |
| `đệm` | `mình` |
| `đệm` | `team` |
| `đệm` | `giúp` |
| `đệm` | `giùm` |
| `đệm` | `dùm` |
| `đệm` | `hộ` |
| `đệm` | `nhờ` |
| `đệm` | `cho` |
| `đệm` | `với` |
| `đệm` | `nha` |
| `đệm` | `nhé` |
| `đệm` | `nhe` |
| `đệm` | `nhá` |
| `đệm` | `ạ` |
| `đệm` | `ơi` |
| `đệm` | `đi` |
| `đệm` | `này` |
| `đệm` | `cái` |
| `đệm` | `đây` |
| `đệm` | `link` |
| `đệm` | `pr` |
| `đệm` | `pull` |
| `đệm` | `request` |
| `đệm` | `issue` |
| `đệm` | `please` |
| `đệm` | `pls` |

## Bảng: link_only

Lọc local **trước khi gọi model**, cùng phạm vi với bảng `prefilter`. Khớp ⇒ `CHAT` ngay với luật `F3`, không gọi model, không ticket, không hỏi. Nhóm nhiều tin chỉ thành `CHAT` khi **mọi** tin đều khớp.

| # | Bước |
|---|---|
| W1 | Tin mới: không phải Reply, không forward, không command, người gửi không đính kèm ảnh hay tài liệu (ảnh/tệp xem trước mà Telegram tự tạo cho link không tính là đính kèm) |
| W2 | Tin có ít nhất một link: bắt đầu bằng `http://`, `https://` hoặc `www.`; đuôi link dừng ở `,` `;`, khoảng trắng và chữ có dấu (chữ dính sau link như "…/checkout,lỗi" vẫn là chữ) |
| W3 | Bỏ ký tự vô hình và mọi link ở W2; phần còn lại không có chữ cái hay chữ số nào (chỉ dấu câu, ký hiệu, emoji hoặc khoảng trắng) |
| W4 | Khớp W1–W3 ⇒ `CHAT` (luật `F3`); có thêm bất kỳ chữ nào (vd "lỗi", "check", "xem") ⇒ không khớp, đi tiếp như cũ |

## Bảng: chat_only

Lọc local **trước khi gọi model**, cùng phạm vi với bảng `link_only`. Khớp ⇒ `CHAT` ngay với luật `F4`, không gọi model, không ticket, không hỏi. Nhóm nhiều tin chỉ thành `CHAT` khi **mọi** tin đều khớp. Xét sau `prefilter` (F1) và `link_only` (F3).

| # | Bước |
|---|---|
| C1 | Tin mới: không phải Reply, không forward, không command, người gửi không đính kèm ảnh hay tài liệu, có chữ (không rỗng) |
| C2 | Bỏ ký tự vô hình; bỏ mọi `@tên`; đổi dấu câu, emoji, ký hiệu và `_` thành khoảng trắng; chữ thường; tách theo khoảng trắng |
| C3 | Tin không có dấu `?` (câu hỏi như "xong rồi à anh?" đi tiếp như cũ); mọi từ còn lại thuộc bảng `chat_words`, hoặc không còn từ nào (tin chỉ có emoji/dấu câu); một chữ số hay từ ngoài bảng ⇒ không khớp |
| C4 | Khớp C1–C3 ⇒ `CHAT` (luật `F4`); `không`, `được`, `chưa`, `lỗi` không có trong bảng nên "vẫn không được" đi tiếp như cũ |

## Bảng: chat_words

Từ xã giao (Owner duyệt 2026-11-06). Thêm từ: một dòng mới, đúng nhóm, chữ thường, chỉ chữ cái. Không thêm từ phủ định hay từ mô tả lỗi.

| Nhóm | Từ |
|---|---|
| `đồng ý` | `ok` |
| `đồng ý` | `oke` |
| `đồng ý` | `okie` |
| `đồng ý` | `oki` |
| `đồng ý` | `okay` |
| `đồng ý` | `dạ` |
| `đồng ý` | `vâng` |
| `đồng ý` | `ừ` |
| `đồng ý` | `ừm` |
| `đồng ý` | `uh` |
| `đồng ý` | `ờ` |
| `đồng ý` | `yes` |
| `đồng ý` | `yep` |
| `đồng ý` | `đúng` |
| `đồng ý` | `chuẩn` |
| `cảm ơn` | `cảm` |
| `cảm ơn` | `cám` |
| `cảm ơn` | `ơn` |
| `cảm ơn` | `thanks` |
| `cảm ơn` | `thank` |
| `cảm ơn` | `thx` |
| `cảm ơn` | `tks` |
| `cảm ơn` | `ty` |
| `xong` | `rồi` |
| `xong` | `xong` |
| `xong` | `ổn` |
| `xong` | `tốt` |
| `xong` | `good` |
| `xong` | `nice` |
| `cười` | `haha` |
| `cười` | `hihi` |
| `cười` | `hehe` |
| `cười` | `kk` |
| `cười` | `kkk` |
| `cười` | `lol` |
| `đệm` | `anh` |
| `đệm` | `em` |
| `đệm` | `chị` |
| `đệm` | `mình` |
| `đệm` | `bạn` |
| `đệm` | `team` |
| `đệm` | `ạ` |
| `đệm` | `nha` |
| `đệm` | `nhé` |
| `đệm` | `nhe` |
| `đệm` | `nhá` |
| `đệm` | `à` |
| `đệm` | `ơi` |
| `đệm` | `luôn` |
| `đệm` | `nhiều` |
| `đệm` | `lắm` |
| `đệm` | `nè` |

## Bảng: banter_only

Lọc local **trước khi gọi model**, cùng phạm vi với bảng `chat_only`. Khớp ⇒ `CHAT` ngay với luật `F5`. Xét sau `prefilter` (F1), `link_only` (F3) và `chat_only` (F4). Nhóm nhiều tin chỉ thành `CHAT` khi **mọi** tin đều khớp. Bối cảnh lấy từ các tin đã lưu của chính group đó; không có bối cảnh (tin đầu tiên, bot mới chạy) ⇒ không khớp.

| # | Bước |
|---|---|
| B1 | Tin mới: không phải Reply, không forward, không command, người gửi không đính kèm ảnh hay tài liệu, không tag bot, có chữ |
| B2 | Bỏ ký tự vô hình và mọi `@tên`; chữ thường. Tin phải có ít nhất một dấu cười: một dấu loại `emoji` trong bảng `banter_signals` xuất hiện trong tin, hoặc một từ thuộc nhóm `cười` của bảng `chat_words`; và không có từ nào loại `vấn đề` của bảng `banter_signals` |
| B3 | Bối cảnh: tối đa 8 tin ngay trước tin đầu tiên đang xét trong cùng group, gửi trong 15 phút trước tin đó. Một tin bối cảnh là "đang đùa" khi thỏa trọn B2 (có dấu cười và không có từ loại `vấn đề`; tin cười về sự cố không tính) và người gửi biết được |
| B4 | Bối cảnh đang đùa khi có ít nhất 2 tin "đang đùa" do ít nhất 2 người gửi khác nhau |
| B5 | Khớp B1–B4 ⇒ `CHAT` (luật `F5`), không gọi model, không ticket, không hỏi; thiếu một điều kiện ⇒ đi tiếp như cũ. Tin không có dấu cười (vd "Thiếu tính năng fake mắt xem rồi") không bao giờ khớp ở đây: model đọc `read_only_context` theo hướng dẫn trong `facts.md` |

## Bảng: banter_signals

Dấu hiệu cho `banter_only` (Owner 2026-11-09). Loại `emoji`: khớp nếu chuỗi này xuất hiện trong tin. Loại `vấn đề`: từ chữ thường; có một từ này trong tin thì tin không bao giờ bị F5 chặn (báo lỗi thật vẫn tới model). Không thêm từ tả tính năng (thiếu, thêm, cần) vào loại `vấn đề`.

| Loại | Dấu |
|---|---|
| `emoji` | `😂` |
| `emoji` | `🤣` |
| `emoji` | `😆` |
| `emoji` | `😁` |
| `emoji` | `😄` |
| `emoji` | `😜` |
| `emoji` | `😝` |
| `emoji` | `🤪` |
| `emoji` | `😹` |
| `emoji` | `=))` |
| `emoji` | `:))` |
| `vấn đề` | `lỗi` |
| `vấn đề` | `bug` |
| `vấn đề` | `crash` |
| `vấn đề` | `treo` |
| `vấn đề` | `đơ` |
| `vấn đề` | `sập` |
| `vấn đề` | `hỏng` |
| `vấn đề` | `không` |
| `vấn đề` | `ko` |
| `vấn đề` | `chưa` |
| `vấn đề` | `chậm` |
| `vấn đề` | `sai` |
| `vấn đề` | `fix` |
| `vấn đề` | `sửa` |
| `vấn đề` | `kẹt` |
| `vấn đề` | `văng` |
| `vấn đề` | `k` |
| `vấn đề` | `hông` |
| `vấn đề` | `hong` |
| `vấn đề` | `khong` |
| `vấn đề` | `loi` |
| `vấn đề` | `chua` |
| `vấn đề` | `cham` |
| `vấn đề` | `mất` |
| `vấn đề` | `trừ` |
| `vấn đề` | `tiền` |
| `vấn đề` | `ví` |
| `vấn đề` | `nạp` |
| `vấn đề` | `rút` |
| `vấn đề` | `đơn` |
| `vấn đề` | `dư` |
| `vấn đề` | `âm` |
| `vấn đề` | `thanh` |
| `vấn đề` | `toán` |
| `vấn đề` | `trắng` |
| `vấn đề` | `lag` |
| `vấn đề` | `error` |
| `vấn đề` | `fail` |
| `vấn đề` | `failed` |
| `vấn đề` | `timeout` |
| `vấn đề` | `die` |
| `vấn đề` | `thất` |
| `vấn đề` | `bại` |
| `vấn đề` | `500` |
| `vấn đề` | `404` |
| `vấn đề` | `đứng` |

## Bảng: team_request

Xét **sau model**, sau bảng `intent_resolve`, **trước** bảng `decision`. Chỉ dựa vào dữ kiện code tính và `intent`; model không biết luật này. Khớp ⇒ `CHAT` với luật `F2`: không ticket, không hỏi. Không khớp ⇒ đi tiếp bảng `decision` như cũ.

| # | Bước |
|---|---|
| T1 | Chủ đề mới: không có command, `extraction_ok` = `true`, `reply_target` = `none`; không phải tin nối vào chủ đề đang chờ trả lời câu hỏi của bot, không phải bước kiểm lại ticket liên quan (recurrence) |
| T2 | `intent` sau bảng `intent_resolve` là `task` (gồm `question` có `work` ở I1) |
| T3 | **Mọi** tin model lấy làm nguồn cho `intent` hoặc `work` đều là tin **không forward** có `@tên` Telegram: `@` + chữ cái Latin + 4–31 chữ cái Latin, số hoặc `_`; không dính sau chữ, số, `@` hay `.` (email không tính). Trước khi tìm: bỏ ký tự vô hình, bỏ đoạn trong backtick, bỏ mọi cụm không khoảng trắng có `/` (link như `tiktok.com/@shop`, gói như `@org/pkg`) |
| T4 | Không tin nào của chủ đề tag tài khoản bot (cờ `mentioned` của Telegram) |
| T5 | Khớp T1–T4 ⇒ `CHAT` (luật `F2`). `bug`, `feedback`, lệnh `/task` và tin có tag bot vẫn đi bảng `decision` |

## Bảng: intent_resolve

Tính `intent` dùng cho bảng `decision` (Owner chốt 2026-10-29: command quyết trước model).

| # | command | intent (model) | cần CÓ | intent dùng |
|---|---|---|---|---|
| I1 | `∅` | `question` | `work` | `task` |
| I2 | `∅` | * | — | `=model` |
| I3 | * | * | — | `=command` |

`=model`: lấy `intent` model trả (ngoài danh sách ⇒ `unknown`). `=command`: tra `facts.md` · `command_intent`.

## Bảng: decision

| # | command | command_payload | text | media | extraction_ok | reply_target | intent | cần CÓ tất cả | cần CÓ ít nhất một | Kết quả | Ghi chú |
|---|---|---|---|---|---|---|---|---|---|---|---|
| R1 | `/question` | * | * | * | * | * | * | — | — | `CHAT` | TG-09 |
| R2 | * | `empty` | * | * | * | * | * | — | — | `CHAT` | Command không nội dung là noise (TG-04) |
| R3 | `∅` | * | `không` | `có` | * | `none` | * | — | — | `MEDIA_ONLY` | Ảnh không chữ: im lặng, giữ pending (TG-15). Không cần model |
| R4 | * | * | * | * | `false` | * | * | — | — | `HOLD` | Lỗi của bot, không hỏi người gửi |
| R5 | * | * | * | * | * | `ticket_open` | `chat` | — | — | `CHAT` | "Cảm ơn", "dạ" vào ticket: bỏ qua |
| R6 | * | * | * | * | * | `ticket_open` | * | — | — | `APPEND` | Luôn bổ sung, không xét R9–R11 (Owner chốt 2026-10-29) |
| R7 | * | * | * | * | * | * | `chat` | — | — | `CHAT` | |
| R8 | * | * | * | * | * | * | `question` | — | — | `CHAT` | Câu hỏi không kèm việc (có việc đã thành `task` ở I1) |
| R9 | * | * | * | * | * | * | `bug` | `object` `observed` | — | `READY` | Thiếu `expected` ⇒ mặc định "hoạt động đúng như trước", không hỏi |
| R10 | * | * | * | * | * | * | `task` | `object` `work` | — | `READY` | Redesign panel/mode đã nhận diện là `READY` |
| R11 | * | * | * | * | * | * | `feedback` | — | `problem` `proposal` | `READY` | |
| R12 | * | * | * | * | * | * | `bug` `task` `feedback` | — | — | `NEED_INFO` | Hỏi trường thiếu theo `questions.md` · `ask_order` |
| R13 | * | * | * | * | * | * | * | — | — | `NEED_INFO` | Gồm `unknown`: câu mặc định `questions.md` · `fallback` |

## Bảng: never_blocks

Thiếu những thứ dưới đây **không bao giờ** làm mất `READY`. Có thì ghi vào ticket; thiếu thì liệt kê ở mục "Cần làm rõ" của ticket, không hỏi.

| Thứ không chặn |
|---|
| thiết bị, trình duyệt, bản build |
| log, mã lỗi chi tiết |
| bước tái hiện |
| tài khoản thử |
| deadline, người phụ trách, thứ tự ưu tiên |
| bản thiết kế/layout tương lai (task redesign) |
| `expected` |
| ảnh model chưa xem (`media_unseen` > 0) |
| điểm `confidence` của model |

## Bảng: unseen_media

Owner chốt D7 = A (2026-10-29).

| # | Quy tắc |
|---|---|
| U1 | `media_unseen` không tham gia bảng `decision`; kết quả chỉ dựa trên trường CÓ từ chữ và ảnh đã xem |
| U2 | Ticket `READY`/`APPEND` đính **mọi** ảnh của chủ đề (kể cả ảnh model chưa xem) và ghi "model chưa xem N ảnh" |
| U3 | Giá trị chỉ đọc được từ ảnh chưa xem không tồn tại (V4), nên không thể làm trường thành CÓ |

## Bảng: confidence

Owner chốt = A (2026-10-29).

| # | Quy tắc |
|---|---|
| C1 | Điểm `confidence` của model **không** tham gia quyết định |
| C2 | Bot vẫn ghi `confidence` vào nhật ký quyết định để theo dõi chất lượng model |

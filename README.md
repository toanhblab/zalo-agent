# Zalo Agent

Điều khiển và đọc Zalo desktop trên macOS qua Chrome DevTools Protocol, kèm
lớp quyết định System One (Jev) cho những lựa chọn cần phán đoán.

Zalo bản macOS là ứng dụng Electron, nên toàn bộ giao diện của nó là DOM. Công
cụ này nói chuyện thẳng với DOM đó. Không OCR, không đoán toạ độ chuột, không
mô phỏng bàn phím. Mọi thứ đọc ra đều là dữ liệu thật đã có cấu trúc.

## Kiến trúc

Nguyên tắc mượn từ Third Hand: **model không bao giờ được đặt tên mục tiêu.**

| Lớp | Việc | Tất định? |
|---|---|---|
| `cdp.py` | Nói WebSocket với Electron, chạy JS trong trang | Có |
| `zalo.py` | Liệt kê hội thoại, mở nhóm, đọc tin nhắn | Có |
| `jev.py` | Chọn một phương án trong tập đóng, chấm xác suất | Không |
| `agent.py` | Ghép hai lớp trên, kiểm lại lựa chọn của model | Có |

Python dựng tập lựa chọn từ các hội thoại có thật, Jev chỉ được chọn một mục
trong tập đó, rồi Python đối chiếu lại lựa chọn với danh sách thật trước khi
hành động. Jev trả về một khoá ngoài tập là lỗi, không phải kết quả.

## Cài đặt

```sh
cd zalo-agent
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python websockets requests
```

## Mở Zalo kèm cổng gỡ lỗi

```sh
env -u ELECTRON_RUN_AS_NODE open -a /Applications/Zalo.app \
    --args --remote-debugging-port=9222
```

Phải gỡ `ELECTRON_RUN_AS_NODE`. Terminal tích hợp của VS Code đặt sẵn biến này,
và khi còn nó thì Zalo khởi động ở chế độ Node rồi báo `bad option`.

Kiểm tra cổng đã mở:

```sh
curl -s http://127.0.0.1:9222/json/version
```

## Dùng

```sh
.venv/bin/python agent.py groups            # liệt kê mọi nhóm
.venv/bin/python agent.py groups hblab      # lọc theo tên
.venv/bin/python agent.py open  "nhóm onsite korea"
.venv/bin/python agent.py find  "nhóm onsite korea" "lịch bay"
.venv/bin/python agent.py ask   "nhóm cờ" "bàn cờ giá bao nhiêu"
```

`groups`, `open` và `find` chạy hoàn toàn tất định. Chỉ `ask` và phần chọn
nhóm khi tên mơ hồ mới gọi tới model.

## Khoá API

`jev.py` tự đọc khoá bên trong tiến trình, theo thứ tự:

1. Biến môi trường `TYPESAFE_API_KEY`
2. macOS Keychain, service `com.thirdhand.openrouter`, account `api-key`

Khoá không bao giờ được ghi ra log hay in ra màn hình. Nếu không có khoá, công
cụ vẫn chạy và tự lùi về so khớp chuỗi cục bộ.

Mục Keychain do Third Hand.app tạo chỉ cho phép chính ứng dụng đó đọc. Muốn
dùng lại từ đây, cấp quyền một lần:

```sh
security find-generic-password -s com.thirdhand.openrouter -a api-key -w
```

Bấm **Always Allow** trong hộp thoại. Hoặc đơn giản hơn là đặt biến môi trường.

## Kiểm thử

```sh
.venv/bin/python test_jev_contract.py   # không cần mạng, không cần khoá
.venv/bin/python test_e2e.py            # cần Zalo đang mở kèm cổng 9222
.venv/bin/python test_ask.py            # chấm điểm tin nhắn thật
```

Hai test đầu cuối dựng một Jev giả lập cục bộ, nên chúng chạy được mà không
tiêu tốn lượt gọi API, trong khi dữ liệu Zalo vẫn là dữ liệu thật.

`test_jev_contract.py` dựng một máy chủ giả lập nói đúng hợp đồng của TypeSafe
để kiểm tra hình dạng request, ngân sách byte, chặn lựa chọn ngoài schema,
nhánh không-cái-nào-hợp, và thang xác suất.

## Hợp đồng DOM

Mọi selector nằm gọn trong `SEL` ở đầu `zalo.py`, để khi Zalo cập nhật giao
diện thì chỉ phải sửa một chỗ.

| Ý nghĩa | Selector |
|---|---|
| Dòng hội thoại | `.msg-item` |
| Mã hội thoại | thuộc tính `anim-data-id`, tiền tố `g` là nhóm |
| Tên hội thoại | `.conv-item-title__name .truncate` |
| Tiêu đề khung chat | `#header .header-title` |
| Tin nhắn | `[id^="bb_msg_id_"]` |
| Nội dung tin | `span.text` |
| Người gửi | `.message-sender-name-content` |

Hai danh sách của Zalo đều ảo hoá bằng react-virtualized, nên phần tử truy vấn
được không phải phần tử cuộn. Hàm `__zScroller` xử lý việc này: khung chat cuộn
ở một phần tử con, còn danh sách hội thoại cuộn ở một phần tử tổ tiên.

Zalo chỉ in tên người gửi một lần cho mỗi chuỗi tin liên tiếp của cùng một
người, nên `zalo.py` tự truyền tên đó xuống các tin kế tiếp.

## Cần biết trước khi dùng

- **Cổng gỡ lỗi là cửa mở.** Bất kỳ tiến trình nào trên máy cũng điều khiển
  được Zalo qua cổng 9222. Chỉ bật khi cần, tắt Zalo và mở lại bình thường sau
  khi xong.
- **Công cụ này đọc là chính.** Chỉ `open_conversation` chạm vào giao diện, và
  nó chỉ phát sự kiện chuột lên một dòng đang hiển thị thật. Không có hàm nào
  gửi tin nhắn.
- **Nội dung đọc ra là dữ liệu riêng tư.** Đừng ghi log nguyên văn tin nhắn ra
  nơi dùng chung.
- **Selector sẽ vỡ khi Zalo cập nhật.** Đây là hệ quả của việc bám vào DOM nội
  bộ của một ứng dụng đóng.

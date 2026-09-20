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

### `latest` — tin mới nhất của một người

Trả về **một** tin: tin mới nhất của người gửi đó, kèm mọi tệp người đó gửi
trong cửa sổ `--attach-window` phút ngay sau tin. Đây là bề mặt dành cho ứng
dụng khác gọi, nên nó **không bao giờ gọi Jev** — kể cả khi tên nhóm mơ hồ.

```sh
.venv/bin/python agent.py latest "lớp cambridge" --from "Thu Huyền" \
    --match "ngày học thứ" --json
```

| cờ | mặc định | ý nghĩa |
|---|---|---|
| `--from` | (bắt buộc) | tên hiển thị của người gửi; so khớp bỏ dấu, bỏ hoa thường |
| `--match` | — | chỉ xét tin chứa cụm này |
| `--attach-window` | `90` | gom tệp của cùng người gửi trong bao nhiêu phút sau tin |
| `--days` | `30` | cuộn ngược tối đa bấy nhiêu ngày |
| `--json` | tắt | in JSON thay vì bản đọc cho người |

Thứ tự "mới nhất" lấy từ `timestamp_ms` trong id phần tử, **không** lấy từ
giờ hiển thị: Zalo chỉ in giờ trên bong bóng cuối mỗi cụm tin.

### `attachments` — kéo tệp của một người về máy

```sh
.venv/bin/python agent.py attachments "lớp cambridge" --from "Thu Huyền" \
    --since 2026-09-01 --out ~/zalo-tep
```

Lệnh này chỉ bấm đúng nút **"Lưu về máy"** có sẵn của Zalo rồi chép tệp ra khỏi
kho cục bộ của chính ứng dụng; nó không dựng URL, không gửi gì. Tệp xếp theo
`<thư mục>/<YYYY-MM-DD>/`, kèm `index.json` ghi tệp nào thuộc tin nào.

Khoá để ghép tin ↔ tệp là nửa sau dấu `@` của `data-qid`, đúng bằng tên tệp
trong kho của Zalo:

```
~/Library/Application Support/ZaloData/media/<ownerId>/ZaloDownloads/
    resource/<groupId>/file/<sentMs>_<cliMsgId>_<groupId>
    resource/<groupId>/video/<sentMs>_<cliMsgId>_<groupId>
    resource/<groupId>/picture/<sentMs>_<cliMsgId>_<groupId>_<hash>.jxl
```

## Ràng buộc vận hành

Ba thứ dưới đây quyết định việc chạy tự động có ra kết quả hay không.

- **Zalo phải đang chạy, đã đăng nhập, kèm cổng 9222.** Không có tiến trình
  Zalo thì không có gì để đọc; công cụ này đọc giao diện chứ không gọi API.
- **Tệp hết hạn sau khoảng hai tuần.** Quá hạn, Zalo hiện "File không tồn tại"
  và bỏ luôn nút tải — không cách nào lấy lại từ máy này. Muốn giữ tệp thì phải
  chạy `attachments` đều đặn, không thể thu thập hồi tố.
- **Phải mở lại nhóm mỗi lần chạy.** Cổng gỡ lỗi điều khiển đúng cái Zalo mà
  người dùng đang dùng, nên khung chat có thể đổi sang hội thoại khác bất cứ
  lúc nào. Mọi lệnh đều gọi `ensure_conversation` để đối chiếu
  `#header .header-title` và mở lại nếu lệch; nếu bỏ bước này, công cụ sẽ đọc
  nhầm hội thoại mà không báo lỗi.

Ngoài ra, lịch sử hội thoại **không có sẵn**: Zalo chỉ nạp tin cũ hơn khi khung
chat bị ghim ở `scrollTop === 0`. `read_messages(deep=True)` làm việc đó, nên
một lần quét sâu tốn vài chục giây; dùng `--days` để giới hạn.

## Khoá API

`jev.py` tự đọc khoá bên trong tiến trình, theo thứ tự:

1. Biến môi trường `TYPESAFE_API_KEY`
2. macOS Keychain, service `com.toanhblab.zalo-agent`, account `typesafe-api-key`

Khoá không bao giờ được ghi ra log hay in ra màn hình. Nếu không có khoá, công
cụ vẫn chạy và tự lùi về so khớp chuỗi cục bộ.

Nạp khoá vào Keychain cho dự án này:

```sh
security add-generic-password -s com.toanhblab.zalo-agent -a typesafe-api-key -w '<KHOÁ>' -U
```

Kiểm lại mà không lộ khoá ra màn hình (chỉ in số ký tự):

```sh
security find-generic-password -s com.toanhblab.zalo-agent -a typesafe-api-key -w | wc -c
```

Biến môi trường `TYPESAFE_API_KEY` vẫn được ưu tiên hơn mục Keychain.

## Kiểm thử

```sh
.venv/bin/python test_jev_contract.py     # không cần mạng, không cần khoá
.venv/bin/python test_latest_contract.py  # thuần Python, không cần Zalo
.venv/bin/python test_e2e.py              # cần Zalo đang mở kèm cổng 9222
.venv/bin/python test_ask.py              # chấm điểm tin nhắn thật
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
| Nội dung tin | `[data-component="message-text-content"]` |
| Người gửi | `.message-sender-name-content` |
| Khoá tệp của tin | thuộc tính `data-qid`, phần sau dấu `@` |
| Bong bóng tệp | `.file-message-v2` |
| Nút lưu tệp | `a.file-message__actions.download` |
| Ảnh / video | `[data-component="photo"]` |

Hai danh sách của Zalo đều ảo hoá bằng react-virtualized, nên phần tử truy vấn
được không phải phần tử cuộn. Hàm `__zScroller` xử lý việc này: khung chat cuộn
ở một phần tử con, còn danh sách hội thoại cuộn ở một phần tử tổ tiên.

Zalo chỉ in tên người gửi một lần cho mỗi chuỗi tin liên tiếp của cùng một
người, nên `zalo.py` tự truyền tên đó xuống các tin kế tiếp. Bỏ bước này thì
phần lớn hội thoại bị gán sai người gửi, chứ không phải mất vài cái tên.

Một tin có **hai dạng id**: `bb_msg_id_<ms>` và
`bb_msg_id_<ms>_<cliMsgId>_<groupId>`, và cùng một tin có thể đổi qua lại giữa
hai dạng giữa các lần render. Chỉ trường đầu là đồng hồ, nên `timestamp_ms` cắt
tại dấu `_` đầu tiên, và khi cần tìm lại bong bóng theo id thì phải tra bằng
tiền tố `[id^="bb_msg_id_<ms>"]`.

Giờ gửi (`.card-send-time__sendTime`) chỉ xuất hiện trên bong bóng cuối mỗi cụm
— khoảng 30% số tin — nên đừng dùng nó để sắp xếp; dùng `timestamp_ms`.

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

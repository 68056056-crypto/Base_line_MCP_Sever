# Seismic MCP Server (Streamable HTTP + Docker)

MCP server สำหรับข้อมูลแผ่นดินไหว (USGS, TMD) และการวิเคราะห์ประกันภัยแบบ parametric
สร้างตาม `spec.md` แต่ใช้ transport เป็น **Streamable HTTP** เป็นค่าเริ่มต้น (ยังรองรับ stdio)

## Tools

| Tool | ทำอะไร |
|---|---|
| `usgs_search_earthquakes` | ค้นหาแผ่นดินไหวจาก USGS FDSN (กรองช่วงเวลา, magnitude, รัศมี) |
| `tmd_get_daily_earthquakes` | แผ่นดินไหวรายวันจาก TMD (ไม่ใส่ date = วันนี้ ตาม `SEISMIC_TZ`) |
| `tmd_get_recent_earthquakes` | รวม N วันล่าสุดจาก TMD (1–7 วัน), ตัดตัวซ้ำ, เรียงเวลาใหม่→เก่า |
| `tmd_search_earthquakes` | **(เพิ่มใหม่)** ค้นข้อมูลกรมอุตุฯ ตั้งแต่ 1 ม.ค. ปีปัจจุบัน กรองช่วงวันที่, magnitude, รัศมีรอบจุด, ชื่อสถานที่ (ไทย/อังกฤษ) |
| `match_events_to_assets` | จับคู่เหตุการณ์กับทรัพย์สินที่เอาประกันตามระยะ (haversine) และ magnitude |
| `compute_parametric_payout` | คำนวณ payout ตาม magnitude trigger + payout curve ตามระยะ |

ทุก tool ที่คืนแผ่นดินไหวใช้ schema `EarthquakeEvent` ตาม spec
ผลลัพธ์ส่งทั้ง `structuredContent` (JSON) และ `content[0].text` (JSON string)

## รันด้วย Docker

```bash
docker compose up -d --build
curl http://localhost:8000/health        # {"status":"ok",...}
```

MCP endpoint: **`http://localhost:8000/mcp`**

หรือไม่ใช้ compose:

```bash
docker build -t seismic-mcp:0.1.0 .
docker run -d --name seismic-mcp -p 8000:8000 seismic-mcp:0.1.0
```

## เชื่อมกับ client

**Claude Code**
```bash
claude mcp add --transport http seismic http://localhost:8000/mcp
```

**Claude Desktop** (ผ่าน bridge `mcp-remote` เพราะไฟล์ config รันเฉพาะ command แบบ stdio)
```json
{
  "mcpServers": {
    "seismic-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8000/mcp"]
    }
  }
}
```
ถ้า deploy ขึ้น URL สาธารณะที่เป็น HTTPS แล้ว สามารถเพิ่มเป็น custom connector ใน Claude ได้โดยตรง

**MCP Inspector**
```bash
npx @modelcontextprotocol/inspector
# เลือก Transport = Streamable HTTP, URL = http://localhost:8000/mcp
```

## Environment variables

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` | หรือ `stdio` |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | bind address |
| `MCP_PATH` | `/mcp` | path ของ endpoint |
| `MCP_STATELESS` | `false` | `true` = ไม่เก็บ session (เหมาะกับหลาย replica หลัง load balancer) |
| `MCP_JSON_RESPONSE` | `false` | `true` = ตอบเป็น JSON แทน SSE stream |
| `ALLOWED_HOSTS` | (ว่าง) | รายการ Host header ที่อนุญาต เปิดกันการโจมตีแบบ DNS rebinding; ว่าง = ปิด |
| `ALLOWED_ORIGINS` | (ว่าง) | Origin ที่อนุญาต (ใช้คู่กับ `ALLOWED_HOSTS`) |
| `SEISMIC_TZ` | `Asia/Bangkok` | timezone ที่ใช้คำนวณ "วันนี้" ของ TMD |
| `HTTP_TIMEOUT` | `20` | timeout เรียก API ภายนอก (วินาที) |
| `TMD_CACHE_TTL` | `60` | อายุ cache ของข้อมูล TMD (วินาที) |
| `LOG_LEVEL` | `INFO` | log ออก stderr เท่านั้น |

ถ้าเปิดให้เครื่องอื่นเรียกได้ ให้เพิ่ม hostname จริงใน `ALLOWED_HOSTS` (เช่น `mcp.example.com`)
และควรวางหลัง reverse proxy ที่มี TLS + authentication เพราะ server นี้ยังไม่มี auth ในตัว

## รันแบบไม่ใช้ Docker

```bash
pip install -r requirements.txt
python seismic_mcp_server.py                      # Streamable HTTP :8000/mcp
MCP_TRANSPORT=stdio python seismic_mcp_server.py  # stdio (Claude Desktop แบบเดิม)
```

## ทดสอบ

```bash
pip install pytest
pytest -q tests/test_server.py                         # unit tests (mock API, ไม่ต้องใช้เน็ต)
python tests/smoke_test.py http://localhost:8000/mcp   # end-to-end ผ่าน Streamable HTTP
python tests/smoke_test.py http://localhost:8000/mcp --live   # เรียก USGS/TMD จริงด้วย
```

## ส่วนที่ต่างจาก spec

- **Transport**: Streamable HTTP เป็นค่าเริ่มต้น (spec เดิมเป็น stdio); stdio ยังใช้ได้ผ่าน `MCP_TRANSPORT=stdio`
- **Error handling** แยกตามมาตรฐาน MCP:
  - argument ผิด schema / tool ไม่มีอยู่ → JSON-RPC error `-32602` (ตาม spec)
  - API ภายนอกล่มหรือตอบผิดรูป → คืน `isError: true` ใน result แทน JSON-RPC error เพื่อให้โมเดลเห็นข้อผิดพลาดและตัดสินใจต่อได้
- **Capabilities / protocolVersion**: SDK ทางการจัดการ negotiation ให้ (`capabilities.tools`, version ล่าสุดที่ทั้งสองฝั่งรองรับ) แทนค่า `tools.list/tools.call` ใน spec ซึ่งไม่ใช่รูปแบบมาตรฐาน
- **TMD API จริงต่างจากตัวอย่างใน spec** (ตรวจกับ API จริงเมื่อ 8 พ.ย. 2026):
  - รายการอยู่ใต้คีย์ `DailyEarthquakes` (ไม่ใช่ `DailySeismicEvent`)
  - ไม่มี `EventID` → server สร้าง id จากเวลา UTC + พิกัด เช่น `TMD-20261108T083635Z_18.824_96.436`
  - เวลาเป็น `DateTimeThai` / `DateTimeUTC` แบบไม่มี timezone → แปลงเป็น ISO8601 `+07:00`
  - `OriginThai` ถูกตัดที่ ~50 ตัวอักษร → ใช้ `TitleThai` (ตัดคำว่า "แผ่นดินไหว" ออก) เป็น `place`
  - ค่าตัวเลขเป็น string → แปลงเป็น number
  - **พารามิเตอร์ `Date` ถูกละเลย** API คืนทุกเหตุการณ์ตั้งแต่ 1 ม.ค. เสมอ (~1,400 รายการ) → server ยังส่ง `Date` ตาม spec แต่กรองตามวันที่ไทยเอง และ cache ผลไว้ `TMD_CACHE_TTL` วินาที (ค่าเริ่มต้น 60) ทำให้ `tmd_get_recent_earthquakes` เรียก API ครั้งเดียวแทน N ครั้ง
  - ยังรองรับรูปแบบตามตัวอย่างใน spec ด้วย
- **เพิ่มเติม**: `compute_parametric_payout` คืน `triggered` ด้วย; payout curve จะถูกเรียงตามระยะก่อนเลือก band; radius search ของ USGS ต้องใส่ `latitude`, `longitude`, `maxradiuskm` ครบทั้งสามตัว

# Installation

## 1) วางโมดูล

- นำโฟลเดอร์ `stock_monthly_report` ไปไว้ใน addons path ของ Odoo

## 2) ตั้งค่า PDF ผ่าน Reverse Proxy (สำคัญ)

ไปที่ `Settings → Technical → Parameters → System Parameters`

- `web.base.url` = URL ภายนอกที่ผู้ใช้เข้า (เช่น `https://your-domain.com`)
- `report.url` = URL ภายในที่ server เรียก Odoo ได้ (เช่น `http://127.0.0.1:8069`)

ใน `odoo.conf`

- `proxy_mode = True`

จากนั้น restart Odoo

## 3) ติดตั้ง wkhtmltopdf

- ต้องเป็น wkhtmltopdf แบบ patched qt

ตรวจสอบด้วย

```bash
wkhtmltopdf --version
```

## 4) อัปเดตโมดูล

### ผ่าน UI

- Apps → ค้นหา `stock_monthly_report` → Upgrade

### ผ่าน CLI

```bash
/var/odoo/odoo15/odoo-bin -c /etc/odoo.conf -d <DB_NAME> -u stock_monthly_report --stop-after-init
```


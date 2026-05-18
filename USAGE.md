# Usage

## เปิดหน้ารายงาน

- เมนู: Inventory → Monthly Stock Report → Generate Report

## ฟิลด์สำคัญ

- Year: ปีของช่วงรายงาน
- Start Month / End Month: เลือกช่วงเดือน
- Location: เลือกคลัง/ที่เก็บ (internal)
- Include Sub-locations: รวม sub-location ใต้ location ที่เลือก
- Product Category / Products: เลือกได้อย่างใดอย่างหนึ่ง (ถ้าเลือก Products จะ override Category)
- Include Zero Qty: รวมรายการที่ qty = 0 (ปกติแนะนำปิดเพื่อ performance)
- Summary Only: แสดงเฉพาะยอดรวม ไม่โชว์รายบรรทัดสินค้า
- Show Movements: แสดง Vendor In / Customer Out / Internal movement

## ดาวน์โหลดไฟล์

- เลือก PDF อย่างเดียว → ดาวน์โหลด PDF
- เลือก Excel อย่างเดียว → ดาวน์โหลด XLSX
- เลือก PDF + Excel พร้อมกัน → ดาวน์โหลด ZIP (มีทั้ง PDF และ XLSX)

## ส่งอีเมล

- ติ๊ก Send by Email
- ใส่ Recipients หรือให้ระบบอ่านจาก system parameter `stock_monthly_report.recipients`


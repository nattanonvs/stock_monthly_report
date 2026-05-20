# -*- coding: utf-8 -*-
import calendar
from collections import defaultdict

from odoo import api, models, fields, _
from odoo.exceptions import UserError


class ReportStockMonthlyPDF(models.AbstractModel):
    _name = "report.stock_monthly_report.report_stock_monthly_pdf"
    _description = "Stock Monthly PDF Report (Grouped + FIFO + Movements)"

    @api.model
    def _get_report_values(self, docids, data=None):
        """
        docids: stock.quant ids (ที่ wizard ส่งมา)
        data: ต้องมี wizard_id เพื่อเอา filter/period ไปแสดงหัวรายงาน + ใช้คำนวณ movement
        """
        data = data or {}
        wizard_id = data.get("wizard_id")
        if not wizard_id:
            raise UserError(_("Missing wizard_id in report data."))

        wizard = self.env["stock.monthly.wizard"].browse(wizard_id).exists()
        if not wizard:
            raise UserError(_("Wizard not found."))

        show_movements = bool(getattr(wizard, "show_movements", False))
        summary_only = bool(getattr(wizard, "summary_only", False))

        # ===== Period (เดือนเริ่ม-สิ้นสุด) =====
        try:
            year = int(wizard.year)
        except Exception:
            raise UserError(_("Invalid Year. Please enter a 4-digit year (e.g. 2026)."))
        m_start = int(wizard.month_start)
        m_end = int(wizard.month_end)
        if m_end < m_start:
            raise UserError(_("End Month must be >= Start Month."))

        date_from = fields.Datetime.to_datetime(f"{year}-{m_start:02d}-01 00:00:00")
        last_day = calendar.monthrange(year, m_end)[1]
        date_to = fields.Datetime.to_datetime(f"{year}-{m_end:02d}-{last_day:02d} 23:59:59")

        # ===== Location subtree =====
        location_ids = wizard._get_location_ids()
        if not location_ids:
            raise UserError(_("No locations found under selected location."))

        # ===== Product/Category filters =====
        product_ids = wizard.product_ids.ids
        categ_ids = wizard.categ_ids.ids

        # ===== 1) Qty ณ วันสิ้นงวด (As-of) ด้วย stock_move_line =====
        # ทำให้ผลลัพธ์คงที่ตาม period (ไม่ใช่ realtime จาก stock_quant)
        where = []
        params = []

        if product_ids:
            where.append("m.product_id = ANY(%s)")
            params.append(product_ids)
        elif categ_ids:
            allowed_categ_ids = self.env["product.category"].search([("id", "child_of", categ_ids)]).ids
            where.append(
                "pt.categ_id = ANY(%s)"
            )
            params.append(allowed_categ_ids)

        having = ""
        if not getattr(wizard, "include_zero_qty", False):
            having = "HAVING SUM(m.qty) != 0"

        query_qty_asof = f"""
            WITH moves AS (
                SELECT
                    sml.location_dest_id AS location_id,
                    sml.product_id AS product_id,
                    sml.qty_done AS qty
                FROM stock_move_line sml
                JOIN stock_location dst ON dst.id = sml.location_dest_id
                WHERE sml.state = 'done'
                  AND sml.date <= %s
                  AND dst.usage = 'internal'
                  AND sml.location_dest_id = ANY(%s)

                UNION ALL

                SELECT
                    sml.location_id AS location_id,
                    sml.product_id AS product_id,
                    -sml.qty_done AS qty
                FROM stock_move_line sml
                JOIN stock_location src ON src.id = sml.location_id
                WHERE sml.state = 'done'
                  AND sml.date <= %s
                  AND src.usage = 'internal'
                  AND sml.location_id = ANY(%s)
            )
            SELECT
                m.location_id,
                sl.complete_name AS location_name,
                pt.categ_id,
                pc.complete_name AS category_name,
                m.product_id,
                pt.name AS product_name,
                pt.default_code AS default_code,
                SUM(m.qty) AS qty,
                uom.name AS uom_name
            FROM moves m
            JOIN stock_location sl ON sl.id = m.location_id
            JOIN product_product pp ON pp.id = m.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN product_category pc ON pc.id = pt.categ_id
            JOIN uom_uom uom ON uom.id = pt.uom_id
            {"WHERE " + " AND ".join(where) if where else ""}
            GROUP BY m.location_id, sl.complete_name, pt.categ_id, pc.complete_name, m.product_id, pt.name, pt.default_code, uom.name
            {having}
            ORDER BY sl.complete_name, pc.complete_name, pt.default_code, pt.name
        """

        self.env.cr.execute(query_qty_asof, [date_to, location_ids, date_to, location_ids] + params)
        rows = self.env.cr.dictfetchall()

        if not rows:
            raise UserError(_("No stock data found at period end for the selected filters."))

        # Collect product ids for valuation + movements
        prod_set = sorted({r["product_id"] for r in rows})

        fifo_cost_map = self.env["fifo.service"].compute_fifo_unit_cost_by_location(prod_set, date_to)

        # ===== 3) Movement split: Vendor / Customer / Internal (ช่วงวันที่ที่เลือก) =====
        # นับ qty_done จาก stock_move_line (state done) ภายใน period
        # - vendor_in: source usage supplier -> dest in selected internal subtree
        # - customer_out: source in selected internal subtree -> dest usage customer
        # - internal: source in subtree -> dest in subtree
        move_map = {}
        if show_movements:
            self.env.cr.execute("""
                SELECT
                    sml.product_id,
                    SUM(CASE WHEN src.usage = 'supplier' AND sml.location_dest_id = ANY(%s) THEN sml.qty_done ELSE 0 END) AS vendor_in,
                    SUM(CASE WHEN sml.location_id = ANY(%s) AND dst.usage = 'customer' THEN sml.qty_done ELSE 0 END) AS customer_out,
                    SUM(CASE WHEN sml.location_id = ANY(%s) AND sml.location_dest_id = ANY(%s) THEN sml.qty_done ELSE 0 END) AS internal_move
                FROM stock_move_line sml
                JOIN stock_location src ON src.id = sml.location_id
                JOIN stock_location dst ON dst.id = sml.location_dest_id
                WHERE sml.state = 'done'
                  AND sml.date >= %s AND sml.date <= %s
                  AND sml.product_id = ANY(%s)
                GROUP BY sml.product_id
            """, [location_ids, location_ids, location_ids, location_ids, date_from, date_to, prod_set])

            move_map = {r["product_id"]: r for r in self.env.cr.dictfetchall()}

        # ===== 4) Build nested structure for QWeb (Location -> Category -> Products) =====
        # เพื่อให้ QWeb render เร็วมาก (ไม่ต้อง grouping ใน template)
        nested = defaultdict(lambda: defaultdict(list))

        for r in rows:
            pid = r["product_id"]
            qty = r["qty"] or 0.0
            unit_cost = fifo_cost_map.get((r["location_id"], pid), 0.0)
            value = qty * unit_cost

            mv = move_map.get(pid) or {}
            nested[r["location_name"]][r["category_name"]].append({
                "product_name": r["product_name"],
                "default_code": r.get("default_code") or "",
                "qty": qty,
                "uom": r.get("uom_name") or "",
                "unit_cost": unit_cost,
                "value": value,
                "vendor_in": mv.get("vendor_in", 0.0) or 0.0,
                "customer_out": mv.get("customer_out", 0.0) or 0.0,
                "internal_move": mv.get("internal_move", 0.0) or 0.0,
            })

        # ===== Header info =====
        def _fmt_user(dt):
            if not dt:
                return ""
            local_dt = fields.Datetime.context_timestamp(wizard, dt)
            return fields.Datetime.to_string(local_dt)

        header = {
            "period": f"{year}-{m_start:02d} ถึง {year}-{m_end:02d}",
            "date_from": _fmt_user(date_from),
            "date_to": _fmt_user(date_to),
            "location": ", ".join(wizard.location_ids.mapped("complete_name")) if wizard.location_ids else "",
            "category": ", ".join(wizard.categ_ids.mapped("complete_name")) if wizard.categ_ids else "",
            "products": ", ".join(wizard.product_ids.mapped("display_name")) if wizard.product_ids else "",
            "wizard_create": _fmt_user(wizard.create_date),
            "printed_at": _fmt_user(fields.Datetime.now()),
        }

        return {
            "doc_ids": docids,
            "doc_model": "stock.quant",
            "docs": self.env["stock.quant"].browse(docids),  # ไม่ได้ loop แสดงแล้ว แต่ใส่ไว้ตามมาตรฐาน
            "wizard": wizard,
            "header": header,
            "nested": nested,
            "show_movements": show_movements,
            "summary_only": summary_only,
        }

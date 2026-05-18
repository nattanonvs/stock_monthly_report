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
        year = wizard.year
        m_start = int(wizard.month_start)
        m_end = int(wizard.month_end)
        if m_end < m_start:
            raise UserError(_("End Month must be >= Start Month."))

        date_from = fields.Datetime.to_datetime(f"{year}-{m_start:02d}-01 00:00:00")
        last_day = calendar.monthrange(year, m_end)[1]
        date_to = fields.Datetime.to_datetime(f"{year}-{m_end:02d}-{last_day:02d} 23:59:59")

        # ===== Location subtree =====
        if getattr(wizard, "include_sub_locations", True):
            location_ids = self.env["stock.location"].search([("id", "child_of", wizard.location_id.id)]).ids
        else:
            location_ids = [wizard.location_id.id]
        if not location_ids:
            raise UserError(_("No locations found under selected location."))

        # ===== Product/Category filters =====
        product_ids = wizard.product_ids.ids
        categ_id = wizard.categ_id.id if wizard.categ_id else False

        # ===== 1) Aggregate On-hand by (location, category, product) using SQL =====
        # เราจะเอาเฉพาะ internal subtree ที่เลือก (+ exclude qty=0 ถ้าไม่ได้เลือก include_zero_qty)
        where = ["q.location_id = ANY(%s)"]
        params = [location_ids]

        if not getattr(wizard, "include_zero_qty", False):
            where.append("q.quantity != 0")

        if product_ids:
            where.append("q.product_id = ANY(%s)")
            params.append(product_ids)
        elif categ_id:
            # child_of category: ใช้ parent_path เร็วกว่าใน SQL (Odoo เก็บเป็น /1/2/..)
            where.append("pt.categ_id IN (SELECT id FROM product_category WHERE parent_path LIKE (SELECT parent_path FROM product_category WHERE id=%s) || '%%')")
            params.append(categ_id)

        query_quants = f"""
            SELECT
                q.location_id,
                sl.complete_name as location_name,
                pt.categ_id,
                pc.complete_name as category_name,
                q.product_id,
                pt.name as product_name,
                SUM(q.quantity) as qty,
                uom.name as uom_name
            FROM stock_quant q
            JOIN stock_location sl ON sl.id = q.location_id
            JOIN product_product pp ON pp.id = q.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN product_category pc ON pc.id = pt.categ_id
            JOIN uom_uom uom ON uom.id = pt.uom_id
            WHERE {" AND ".join(where)}
            GROUP BY q.location_id, sl.complete_name, pt.categ_id, pc.complete_name, q.product_id, pt.name, uom.name
            ORDER BY sl.complete_name, pc.complete_name, pt.name
        """

        self.env.cr.execute(query_quants, params)
        rows = self.env.cr.dictfetchall()

        if not rows:
            raise UserError(_("No stock quants found for the selected filters."))

        # Collect product ids for FIFO cost + movements
        prod_set = sorted({r["product_id"] for r in rows})

        # ===== 2) FIFO unit cost from stock_valuation_layer (current remaining) =====
        # unit_cost_fifo = sum(remaining_value) / sum(remaining_qty)
        self.env.cr.execute("""
            SELECT product_id,
                   COALESCE(SUM(remaining_qty), 0) as rem_qty,
                   COALESCE(SUM(remaining_value), 0) as rem_val
            FROM stock_valuation_layer
            WHERE product_id = ANY(%s)
            GROUP BY product_id
        """, [prod_set])
        svl = {r["product_id"]: r for r in self.env.cr.dictfetchall()}

        fifo_cost = {}
        for pid in prod_set:
            rem_qty = (svl.get(pid) or {}).get("rem_qty") or 0.0
            rem_val = (svl.get(pid) or {}).get("rem_val") or 0.0
            fifo_cost[pid] = (rem_val / rem_qty) if rem_qty else 0.0

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
            unit_cost = fifo_cost.get(pid, 0.0)
            value = qty * unit_cost

            mv = move_map.get(pid) or {}
            nested[r["location_name"]][r["category_name"]].append({
                "product_name": r["product_name"],
                "qty": qty,
                "uom": r.get("uom_name") or "",
                "unit_cost": unit_cost,
                "value": value,
                "vendor_in": mv.get("vendor_in", 0.0) or 0.0,
                "customer_out": mv.get("customer_out", 0.0) or 0.0,
                "internal_move": mv.get("internal_move", 0.0) or 0.0,
            })

        # ===== Header info =====
        header = {
            "period": f"{year}-{m_start:02d} ถึง {year}-{m_end:02d}",
            "date_from": date_from,
            "date_to": date_to,
            "location": wizard.location_id.complete_name,
            "category": wizard.categ_id.complete_name if wizard.categ_id else "",
            "products": ", ".join(wizard.product_ids.mapped("display_name")) if wizard.product_ids else "",
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

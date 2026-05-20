# -*- coding: utf-8 -*-
from collections import defaultdict
from odoo import models, fields, _
from odoo.exceptions import UserError


class StockFifoAsofXlsx(models.AbstractModel):
    _name = "report.stock_monthly_report.report_stock_fifo_asof_xlsx"
    _inherit = "report.report_xlsx.abstract"
    _description = "FIFO Valuation As-of Date (XLSX)"

    def generate_xlsx_report(self, workbook, data, objects):
        data = data or {}
        wizard_id = data.get("wizard_id")
        if not wizard_id:
            raise UserError(_("Missing wizard_id"))

        wizard = self.env["stock.fifo.asof.wizard"].browse(wizard_id).exists()
        if not wizard:
            raise UserError(_("Wizard not found"))

        asof = wizard.asof_datetime
        location_ids = wizard._get_location_ids()

        # ---- product filter ----
        product_ids = wizard.product_ids.ids
        categ_ids = wizard.categ_ids.ids

        # =========================
        # 1) Qty at date by product (internal subtree)
        # =========================
        where = ["sml.state='done'", "sml.date <= %s"]
        params = [asof]

        # inbound to subtree (dest in subtree)
        # outbound from subtree (src in subtree)
        where.append("(sml.location_id = ANY(%s) OR sml.location_dest_id = ANY(%s))")
        params.extend([location_ids, location_ids])

        if product_ids:
            where.append("sml.product_id = ANY(%s)")
            params.append(product_ids)

        qty_sql = f"""
            SELECT
                pt.categ_id,
                pc.complete_name AS category,
                sml.product_id,
                pt.name AS product,
                uom.name AS uom,
                SUM(
                    CASE
                      WHEN sml.location_dest_id = ANY(%s) AND dst.usage='internal' THEN sml.qty_done
                      WHEN sml.location_id = ANY(%s) AND src.usage='internal' THEN -sml.qty_done
                      ELSE 0
                    END
                ) AS qty
            FROM stock_move_line sml
            JOIN stock_location src ON src.id = sml.location_id
            JOIN stock_location dst ON dst.id = sml.location_dest_id
            JOIN product_product pp ON pp.id = sml.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN product_category pc ON pc.id = pt.categ_id
            JOIN uom_uom uom ON uom.id = pt.uom_id
            WHERE {" AND ".join(where)}
            GROUP BY pt.categ_id, pc.complete_name, sml.product_id, pt.name, uom.name
            HAVING SUM(
                    CASE
                      WHEN sml.location_dest_id = ANY(%s) AND dst.usage='internal' THEN sml.qty_done
                      WHEN sml.location_id = ANY(%s) AND src.usage='internal' THEN -sml.qty_done
                      ELSE 0
                    END
                ) != 0
            ORDER BY pc.complete_name, pt.name
        """

        # params order: asof, locs, locs, optional products + then locs/locs again for CASE in SELECT & HAVING
        params2 = params.copy()
        # prepend loc arrays used inside CASE (2) + append again for HAVING (2)
        final_params = [location_ids, location_ids] + params2 + [location_ids, location_ids]

        self.env.cr.execute(qty_sql, final_params)
        qty_rows = self.env.cr.dictfetchall()

        if categ_ids:
            allowed_cats = set(self.env["product.category"].search([("id", "child_of", categ_ids)]).ids)
            qty_rows = [r for r in qty_rows if r["categ_id"] in allowed_cats]

        if not qty_rows:
            raise UserError(_("No quantities found at the selected date for given filters."))

        prod_list = sorted({r["product_id"] for r in qty_rows})

        # =========================
        # 2) Value at date from SVL (as-of)
        # =========================
        # สำหรับ automated valuation: มูลค่าคงเหลือ ณ วันที่อ้างอิงมักถูกอิงจาก valuation/report ณ date ที่เลือก
        # ที่นี่เรารวม SVL value ถึง asof ต่อ product แล้วกระจายเป็น unit value โดยหารด้วย qty ณ asof
        self.env.cr.execute("""
            SELECT product_id, COALESCE(SUM(value), 0) AS val
            FROM stock_valuation_layer
            WHERE create_date <= %s
              AND product_id = ANY(%s)
            GROUP BY product_id
        """, [asof, prod_list])
        val_map = {r["product_id"]: r["val"] for r in self.env.cr.dictfetchall()}

        # =========================
        # 3) Build grouped data Category -> lines
        # =========================
        grouped = defaultdict(list)
        for r in qty_rows:
            pid = r["product_id"]
            qty = r["qty"] or 0.0
            total_val = val_map.get(pid, 0.0)
            unit_val = (total_val / qty) if qty else 0.0
            grouped[r["category"]].append({
                "product": r["product"],
                "uom": r["uom"],
                "qty": qty,
                "unit_value": unit_val,
                "value": qty * unit_val,
            })

        # =========================
        # Render XLSX
        # =========================
        sheet = workbook.add_worksheet("FIFO As-of")
        title_fmt = workbook.add_format({"bold": True, "font_size": 14})
        head_fmt = workbook.add_format({"bold": True, "border": 1, "align": "center", "bg_color": "#EFEFEF"})
        text_fmt = workbook.add_format({"border": 1})
        qty_fmt = workbook.add_format({"border": 1, "num_format": "#,##0.00"})
        money_fmt = workbook.add_format({"border": 1, "num_format": "#,##0.00"})
        sub_fmt = workbook.add_format({"bold": True, "top": 2, "num_format": "#,##0.00"})

        sheet.set_column("A:A", 32)
        sheet.set_column("B:B", 10)
        sheet.set_column("C:C", 12)
        sheet.set_column("D:D", 14)
        sheet.set_column("E:E", 14)

        def _fmt_user(dt):
            if not dt:
                return ""
            local_dt = fields.Datetime.context_timestamp(wizard, dt)
            return fields.Datetime.to_string(local_dt)

        sheet.write(0, 0, "FIFO Valuation (As-of Date)", title_fmt)
        sheet.write(1, 0, f"As of: {_fmt_user(asof)}")
        sheet.write(2, 0, "Location (incl. sub): " + ", ".join(wizard.location_ids.mapped("complete_name")))
        if wizard.categ_ids:
            sheet.write(3, 0, "Category: " + ", ".join(wizard.categ_ids.mapped("complete_name")))
        if wizard.product_ids:
            sheet.write(4, 0, "Products: " + ", ".join(wizard.product_ids.mapped("display_name")))
        sheet.write(5, 0, f"Mode: {'SUMMARY' if wizard.summary_only else 'DETAIL'}")

        headers = ["Product", "UoM", "Qty", "Unit Value", "Value"]
        row = 7
        for col, h in enumerate(headers):
            sheet.write(row, col, h, head_fmt)
        row += 1

        grand_qty = grand_val = 0.0
        for cat, lines in grouped.items():
            sheet.write(row, 0, f"Category: {cat}", head_fmt)
            row += 1

            cat_qty = cat_val = 0.0
            if not wizard.summary_only:
                for ln in lines:
                    sheet.write(row, 0, ln["product"], text_fmt)
                    sheet.write(row, 1, ln["uom"], text_fmt)
                    sheet.write_number(row, 2, ln["qty"], qty_fmt)
                    sheet.write_number(row, 3, ln["unit_value"], money_fmt)
                    sheet.write_number(row, 4, ln["value"], money_fmt)
                    cat_qty += ln["qty"]
                    cat_val += ln["value"]
                    row += 1
            else:
                for ln in lines:
                    cat_qty += ln["qty"]
                    cat_val += ln["value"]

            sheet.write(row, 0, "Category Subtotal", sub_fmt)
            sheet.write_number(row, 2, cat_qty, sub_fmt)
            sheet.write_number(row, 4, cat_val, sub_fmt)
            row += 2

            grand_qty += cat_qty
            grand_val += cat_val

        sheet.write(row, 0, "Grand Total", title_fmt)
        sheet.write_number(row, 2, grand_qty, sub_fmt)
        sheet.write_number(row, 4, grand_val, sub_fmt)

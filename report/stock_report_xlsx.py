# -*- coding: utf-8 -*-
import calendar
from collections import defaultdict

from odoo import models, fields, _
from odoo.exceptions import UserError


class StockReportXlsx(models.AbstractModel):
    _name = "report.stock_monthly_report.report_stock_monthly_xlsx"
    _inherit = "report.report_xlsx.abstract"
    _description = "Stock Monthly XLSX Report (Grouped + FIFO + Optional Movements + Summary Mode)"

    def generate_xlsx_report(self, workbook, data, objects):
        data = data or {}
        wizard_id = data.get("wizard_id")
        if not wizard_id:
            raise UserError(_("Missing wizard_id for XLSX report."))

        wizard = self.env["stock.monthly.wizard"].browse(wizard_id).exists()
        if not wizard:
            raise UserError(_("Wizard not found."))

        show_movements = bool(getattr(wizard, "show_movements", False))
        summary_only = bool(getattr(wizard, "summary_only", False))

        # ---------- Period ----------
        try:
            year = int(wizard.year)
        except Exception:
            raise UserError(_("Invalid Year. Please enter a 4-digit year (e.g. 2026)."))
        m_start = int(wizard.month_start)
        m_end = int(wizard.month_end)
        last_day = calendar.monthrange(year, m_end)[1]
        date_from = fields.Datetime.to_datetime(f"{year}-{m_start:02d}-01 00:00:00")
        date_to = fields.Datetime.to_datetime(f"{year}-{m_end:02d}-{last_day:02d} 23:59:59")

        # ---------- Location subtree ----------
        location_ids = wizard._get_location_ids()

        # ---------- Product / Category filters ----------
        product_ids = wizard.product_ids.ids
        categ_ids = wizard.categ_ids.ids

        # ---------- Formats ----------
        title_fmt = workbook.add_format({"bold": True, "font_size": 14})
        header_fmt = workbook.add_format({"bold": True, "border": 1, "align": "center", "bg_color": "#EFEFEF"})
        group_fmt = workbook.add_format({"bold": True, "bg_color": "#E9ECEF"})
        sub_fmt = workbook.add_format({"bold": True, "top": 2, "num_format": "#,##0.00"})
        text_fmt = workbook.add_format({"border": 1})
        qty_fmt = workbook.add_format({"border": 1, "num_format": "#,##0.00"})
        money_fmt = workbook.add_format({"border": 1, "num_format": "#,##0.00"})

        # ---------- Sheet ----------
        sheet = workbook.add_worksheet("Stock On Hand")
        sheet.set_column("A:A", 30)  # Product
        sheet.set_column("B:B", 16)  # Internal Ref
        sheet.set_column("C:C", 12)  # Qty
        sheet.set_column("D:D", 10)  # UoM
        sheet.set_column("E:E", 14)  # Unit cost
        sheet.set_column("F:F", 14)  # Value
        if show_movements:
            sheet.set_column("G:I", 14)

        # ---------- Header text ----------
        def _fmt_user(dt):
            if not dt:
                return ""
            local_dt = fields.Datetime.context_timestamp(wizard, dt)
            return fields.Datetime.to_string(local_dt)

        header_row = 0
        sheet.write(header_row, 0, "Stock On Hand Report", title_fmt)
        header_row += 1
        sheet.write(header_row, 0, f"Period: {year}-{m_start:02d} to {year}-{m_end:02d}")
        header_row += 1
        sheet.write(header_row, 0, "Location (incl. sub): " + ", ".join(wizard.location_ids.mapped("complete_name")))
        header_row += 1
        if wizard.categ_ids:
            sheet.write(header_row, 0, "Category: " + ", ".join(wizard.categ_ids.mapped("complete_name")))
            header_row += 1
        if wizard.product_ids:
            sheet.write(header_row, 0, "Products: " + ", ".join(wizard.product_ids.mapped("display_name")))
            header_row += 1
        sheet.write(header_row, 0, f"Mode: {'SUMMARY' if summary_only else 'DETAIL'} | Movements: {'ON' if show_movements else 'OFF'}")
        header_row += 1
        sheet.write(header_row, 0, f"Stock Balance As-of: {_fmt_user(date_to)}")
        header_row += 1
        if show_movements:
            sheet.write(header_row, 0, f"Movements Period: {_fmt_user(date_from)} to {_fmt_user(date_to)}")
            header_row += 1
        sheet.write(header_row, 0, f"Wizard Created: {_fmt_user(wizard.create_date)}")
        header_row += 1
        sheet.write(header_row, 0, f"Printed At: {_fmt_user(fields.Datetime.now())}")

        # =========================================================
        # 1) Qty ณ วันสิ้นงวด (As-of) ด้วย stock_move_line
        # =========================================================
        where = []
        params = []
        if product_ids:
            where.append("m.product_id = ANY(%s)")
            params.append(product_ids)
        elif categ_ids:
            allowed_categ_ids = self.env["product.category"].search([("id", "child_of", categ_ids)]).ids
            where.append("pt.categ_id = ANY(%s)")
            params.append(allowed_categ_ids)

        having = ""
        if not getattr(wizard, "include_zero_qty", False):
            having = "HAVING SUM(m.qty) != 0"

        self.env.cr.execute(f"""
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
                sl.complete_name AS location,
                pc.complete_name AS category,
                pt.name AS product,
                pt.default_code AS default_code,
                m.product_id,
                SUM(m.qty) AS qty,
                uom.name AS uom
            FROM moves m
            JOIN stock_location sl ON sl.id = m.location_id
            JOIN product_product pp ON pp.id = m.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN product_category pc ON pc.id = pt.categ_id
            JOIN uom_uom uom ON uom.id = pt.uom_id
            {"WHERE " + " AND ".join(where) if where else ""}
            GROUP BY m.location_id, sl.complete_name, pc.complete_name, pt.name, pt.default_code, m.product_id, uom.name
            {having}
            ORDER BY sl.complete_name, pc.complete_name, pt.default_code, pt.name
        """, [date_to, location_ids, date_to, location_ids] + params)

        rows = self.env.cr.dictfetchall()
        if not rows:
            sheet.write(7, 0, "No data found.")
            return

        prod_list = sorted({r["product_id"] for r in rows})

        fifo_cost_map = self.env["fifo.service"].compute_fifo_unit_cost_by_location(prod_list, date_to)

        # =========================================================
        # 3) Movements split (Vendor/Customer/Internal) - OPTIONAL
        #    Filter by period + location subtree (สำคัญ!)
        # =========================================================
        mov_map = {}
        if show_movements:
            self.env.cr.execute("""
                SELECT
                    sml.product_id,
                    SUM(CASE WHEN src.usage='supplier' AND sml.location_dest_id = ANY(%s) THEN sml.qty_done ELSE 0 END) AS vendor_in,
                    SUM(CASE WHEN sml.location_id = ANY(%s) AND dst.usage='customer' THEN sml.qty_done ELSE 0 END) AS customer_out,
                    SUM(CASE WHEN sml.location_id = ANY(%s) AND sml.location_dest_id = ANY(%s) THEN sml.qty_done ELSE 0 END) AS internal_mv
                FROM stock_move_line sml
                JOIN stock_location src ON src.id = sml.location_id
                JOIN stock_location dst ON dst.id = sml.location_dest_id
                WHERE sml.state='done'
                  AND sml.date >= %s AND sml.date <= %s
                  AND sml.product_id = ANY(%s)
                GROUP BY sml.product_id
            """, [location_ids, location_ids, location_ids, location_ids, date_from, date_to, prod_list])

            mov_map = {r["product_id"]: r for r in self.env.cr.dictfetchall()}

        # =========================================================
        # 4) Build nested structure (Location -> Category -> Products)
        # =========================================================
        grouped = defaultdict(lambda: defaultdict(list))
        for r in rows:
            pid = r["product_id"]
            qty = r["qty"] or 0.0
            cost = fifo_cost_map.get((r["location_id"], pid), 0.0)
            value = qty * cost
            mv = mov_map.get(pid) or {}

            grouped[r["location"]][r["category"]].append({
                "product": r["product"],
                "default_code": r.get("default_code") or "",
                "qty": qty,
                "uom": r["uom"],
                "cost": cost,
                "value": value,
                "vendor_in": mv.get("vendor_in", 0.0) or 0.0,
                "customer_out": mv.get("customer_out", 0.0) or 0.0,
                "internal": mv.get("internal_mv", 0.0) or 0.0,
            })

        # =========================================================
        # 5) Dynamic columns (hide movements if not checked)
        # =========================================================
        base_headers = ["Product", "Internal Ref", "Qty", "UoM", "FIFO Unit Cost (As-of)", "Value"]
        move_headers = ["Vendor In", "Customer Out", "Internal"]
        headers = base_headers + (move_headers if show_movements else [])

        start_row = header_row + 2
        for col, h in enumerate(headers):
            sheet.write(start_row, col, h, header_fmt)
        row = start_row + 1

        # =========================================================
        # 6) Render (detail or summary)
        # =========================================================
        def write_line(row_idx, col_idx, key, fmt=None):
            fmt = fmt or text_fmt
            sheet.write(row_idx, col_idx, key, fmt)

        grand_qty = grand_val = 0.0
        grand_vin = grand_cout = grand_int = 0.0

        for loc, cat_map in grouped.items():
            sheet.write(row, 0, f"Location: {loc}", group_fmt)
            row += 1

            loc_qty = loc_val = 0.0
            loc_vin = loc_cout = loc_int = 0.0

            for cat, products in cat_map.items():
                sheet.write(row, 0, f"Category: {cat}", group_fmt)
                row += 1

                cat_qty = cat_val = 0.0
                cat_vin = cat_cout = cat_int = 0.0

                if not summary_only:
                    # ---- DETAIL: write product rows ----
                    for p in products:
                        sheet.write(row, 0, p["product"], text_fmt)
                        sheet.write(row, 1, p["default_code"], text_fmt)
                        sheet.write_number(row, 2, p["qty"], qty_fmt)
                        sheet.write(row, 3, p["uom"], text_fmt)
                        sheet.write_number(row, 4, p["cost"], money_fmt)
                        sheet.write_number(row, 5, p["value"], money_fmt)

                        if show_movements:
                            sheet.write_number(row, 6, p["vendor_in"], qty_fmt)
                            sheet.write_number(row, 7, p["customer_out"], qty_fmt)
                            sheet.write_number(row, 8, p["internal"], qty_fmt)

                        cat_qty += p["qty"]
                        cat_val += p["value"]
                        cat_vin += p["vendor_in"]
                        cat_cout += p["customer_out"]
                        cat_int += p["internal"]
                        row += 1
                else:
                    # ---- SUMMARY: just sum products ----
                    for p in products:
                        cat_qty += p["qty"]
                        cat_val += p["value"]
                        cat_vin += p["vendor_in"]
                        cat_cout += p["customer_out"]
                        cat_int += p["internal"]

                # ---- Category subtotal row ----
                sheet.write(row, 0, "Category Subtotal", sub_fmt)
                sheet.write(row, 1, "", sub_fmt)
                sheet.write_number(row, 2, cat_qty, sub_fmt)
                sheet.write(row, 3, "", sub_fmt)
                sheet.write(row, 4, "", sub_fmt)
                sheet.write_number(row, 5, cat_val, sub_fmt)
                if show_movements:
                    sheet.write_number(row, 6, cat_vin, sub_fmt)
                    sheet.write_number(row, 7, cat_cout, sub_fmt)
                    sheet.write_number(row, 8, cat_int, sub_fmt)
                row += 1

                loc_qty += cat_qty
                loc_val += cat_val
                loc_vin += cat_vin
                loc_cout += cat_cout
                loc_int += cat_int

            # ---- Location subtotal row ----
            sheet.write(row, 0, "Location Subtotal", sub_fmt)
            sheet.write(row, 1, "", sub_fmt)
            sheet.write_number(row, 2, loc_qty, sub_fmt)
            sheet.write(row, 3, "", sub_fmt)
            sheet.write(row, 4, "", sub_fmt)
            sheet.write_number(row, 5, loc_val, sub_fmt)
            if show_movements:
                sheet.write_number(row, 6, loc_vin, sub_fmt)
                sheet.write_number(row, 7, loc_cout, sub_fmt)
                sheet.write_number(row, 8, loc_int, sub_fmt)
            row += 2

            grand_qty += loc_qty
            grand_val += loc_val
            grand_vin += loc_vin
            grand_cout += loc_cout
            grand_int += loc_int

        # ---- Grand total ----
        sheet.write(row, 0, "Grand Total", title_fmt)
        sheet.write(row, 1, "", sub_fmt)
        sheet.write_number(row, 2, grand_qty, sub_fmt)
        sheet.write_number(row, 5, grand_val, sub_fmt)
        if show_movements:
            sheet.write_number(row, 6, grand_vin, sub_fmt)
            sheet.write_number(row, 7, grand_cout, sub_fmt)
            sheet.write_number(row, 8, grand_int, sub_fmt)

        # Note
        row += 2
        sheet.write(row, 0, "Note: FIFO unit cost is simulated per location as-of period end by replaying stock moves (in/out/internal transfer) and valuing inbound layers using Stock Valuation Layers.")

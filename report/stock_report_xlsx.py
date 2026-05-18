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
        year = wizard.year
        m_start = int(wizard.month_start)
        m_end = int(wizard.month_end)
        last_day = calendar.monthrange(year, m_end)[1]
        date_from = fields.Datetime.to_datetime(f"{year}-{m_start:02d}-01 00:00:00")
        date_to = fields.Datetime.to_datetime(f"{year}-{m_end:02d}-{last_day:02d} 23:59:59")

        # ---------- Location subtree ----------
        if getattr(wizard, "include_sub_locations", True):
            location_ids = self.env["stock.location"].search([("id", "child_of", wizard.location_id.id)]).ids
        else:
            location_ids = [wizard.location_id.id]

        # ---------- Product / Category filters ----------
        product_ids = wizard.product_ids.ids
        categ_id = wizard.categ_id.id if wizard.categ_id else False

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
        sheet.set_column("B:B", 12)  # Qty
        sheet.set_column("C:C", 10)  # UoM
        sheet.set_column("D:D", 14)  # FIFO cost
        sheet.set_column("E:E", 14)  # Value
        if show_movements:
            sheet.set_column("F:H", 14)

        # ---------- Header text ----------
        sheet.write(0, 0, "Stock On Hand Report", title_fmt)
        sheet.write(1, 0, f"Period: {year}-{m_start:02d} to {year}-{m_end:02d}")
        sheet.write(2, 0, f"Location (incl. sub): {wizard.location_id.complete_name}")
        if wizard.categ_id:
            sheet.write(3, 0, f"Category: {wizard.categ_id.complete_name}")
        if wizard.product_ids:
            sheet.write(4, 0, "Products: " + ", ".join(wizard.product_ids.mapped("display_name")))
        sheet.write(5, 0, f"Mode: {'SUMMARY' if summary_only else 'DETAIL'} | Movements: {'ON' if show_movements else 'OFF'}")

        # =========================================================
        # 1) Aggregate QUANTS by location/category/product (SQL)
        # =========================================================
        where = ["q.location_id = ANY(%s)"]
        params = [location_ids]

        if not getattr(wizard, "include_zero_qty", False):
            where.append("q.quantity != 0")

        if product_ids:
            where.append("q.product_id = ANY(%s)")
            params.append(product_ids)
        elif categ_id:
            # category subtree by parent_path for speed
            where.append("pt.categ_id IN (SELECT id FROM product_category WHERE parent_path LIKE (SELECT parent_path FROM product_category WHERE id=%s) || '%%')")
            params.append(categ_id)

        self.env.cr.execute(f"""
            SELECT
                sl.complete_name AS location,
                pc.complete_name AS category,
                pt.name AS product,
                q.product_id,
                SUM(q.quantity) AS qty,
                uom.name AS uom
            FROM stock_quant q
            JOIN stock_location sl ON sl.id = q.location_id
            JOIN product_product pp ON pp.id = q.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            JOIN product_category pc ON pc.id = pt.categ_id
            JOIN uom_uom uom ON uom.id = pt.uom_id
            WHERE {" AND ".join(where)}
            GROUP BY sl.complete_name, pc.complete_name, pt.name, q.product_id, uom.name
            ORDER BY sl.complete_name, pc.complete_name, pt.name
        """, params)

        rows = self.env.cr.dictfetchall()
        if not rows:
            sheet.write(7, 0, "No data found.")
            return

        prod_list = sorted({r["product_id"] for r in rows})

        # =========================================================
        # 2) FIFO unit cost (current) from stock_valuation_layer
        #    unit_cost = sum(remaining_value)/sum(remaining_qty)
        # =========================================================
        self.env.cr.execute("""
            SELECT product_id,
                   COALESCE(SUM(remaining_qty), 0) AS rem_qty,
                   COALESCE(SUM(remaining_value), 0) AS rem_val
            FROM stock_valuation_layer
            WHERE product_id = ANY(%s)
            GROUP BY product_id
        """, [prod_list])

        svl = {r["product_id"]: r for r in self.env.cr.dictfetchall()}
        fifo_cost = {}
        for pid in prod_list:
            rq = (svl.get(pid) or {}).get("rem_qty") or 0.0
            rv = (svl.get(pid) or {}).get("rem_val") or 0.0
            fifo_cost[pid] = (rv / rq) if rq else 0.0

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
            cost = fifo_cost.get(pid, 0.0)
            value = qty * cost
            mv = mov_map.get(pid) or {}

            grouped[r["location"]][r["category"]].append({
                "product": r["product"],
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
        base_headers = ["Product", "Qty", "UoM", "FIFO Unit Cost", "Value"]
        move_headers = ["Vendor In", "Customer Out", "Internal"]
        headers = base_headers + (move_headers if show_movements else [])

        start_row = 7
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
                        sheet.write_number(row, 1, p["qty"], qty_fmt)
                        sheet.write(row, 2, p["uom"], text_fmt)
                        sheet.write_number(row, 3, p["cost"], money_fmt)
                        sheet.write_number(row, 4, p["value"], money_fmt)

                        if show_movements:
                            sheet.write_number(row, 5, p["vendor_in"], qty_fmt)
                            sheet.write_number(row, 6, p["customer_out"], qty_fmt)
                            sheet.write_number(row, 7, p["internal"], qty_fmt)

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
                sheet.write_number(row, 1, cat_qty, sub_fmt)
                sheet.write(row, 2, "", sub_fmt)
                sheet.write(row, 3, "", sub_fmt)
                sheet.write_number(row, 4, cat_val, sub_fmt)
                if show_movements:
                    sheet.write_number(row, 5, cat_vin, sub_fmt)
                    sheet.write_number(row, 6, cat_cout, sub_fmt)
                    sheet.write_number(row, 7, cat_int, sub_fmt)
                row += 1

                loc_qty += cat_qty
                loc_val += cat_val
                loc_vin += cat_vin
                loc_cout += cat_cout
                loc_int += cat_int

            # ---- Location subtotal row ----
            sheet.write(row, 0, "Location Subtotal", sub_fmt)
            sheet.write_number(row, 1, loc_qty, sub_fmt)
            sheet.write(row, 2, "", sub_fmt)
            sheet.write(row, 3, "", sub_fmt)
            sheet.write_number(row, 4, loc_val, sub_fmt)
            if show_movements:
                sheet.write_number(row, 5, loc_vin, sub_fmt)
                sheet.write_number(row, 6, loc_cout, sub_fmt)
                sheet.write_number(row, 7, loc_int, sub_fmt)
            row += 2

            grand_qty += loc_qty
            grand_val += loc_val
            grand_vin += loc_vin
            grand_cout += loc_cout
            grand_int += loc_int

        # ---- Grand total ----
        sheet.write(row, 0, "Grand Total", title_fmt)
        sheet.write_number(row, 1, grand_qty, sub_fmt)
        sheet.write_number(row, 4, grand_val, sub_fmt)
        if show_movements:
            sheet.write_number(row, 5, grand_vin, sub_fmt)
            sheet.write_number(row, 6, grand_cout, sub_fmt)
            sheet.write_number(row, 7, grand_int, sub_fmt)

        # Note
        row += 2
        sheet.write(row, 0, "Note: FIFO unit cost here is derived from current Stock Valuation Layers (remaining value/qty).")

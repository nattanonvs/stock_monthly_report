from collections import defaultdict, deque

from odoo import models, fields

class FifoService(models.AbstractModel):
    _name = "fifo.service"
    _description = "FIFO Valuation Service"

    def compute_fifo(self, quants, cutoff_date):
        results = []
        for q in quants:
            layers = self.env['stock.valuation.layer'].search([
                ('product_id', '=', q.product_id.id),
                ('create_date', '<=', cutoff_date)
            ], order='create_date asc')

            qty_to_value = q.quantity
            total_value = 0.0

            for layer in layers:
                if qty_to_value <= 0:
                    break
                layer_qty = layer.remaining_qty
                take_qty = min(qty_to_value, layer_qty)
                total_value += take_qty * (layer.value / layer.quantity)
                qty_to_value -= take_qty

            results.append({
                "product_id": q.product_id.id,
                "quantity": q.quantity,
                "value": total_value,
            })
        return results

    def compute_fifo_unit_cost_by_location(self, product_ids, date_to):
        if not product_ids:
            return {}

        products = self.env["product.product"].browse(product_ids).exists()
        product_uom_map = {p.id: p.uom_id for p in products}

        self.env.cr.execute("""
            SELECT
                sml.id,
                sml.date,
                sml.qty_done,
                sml.product_id,
                sml.move_id,
                sml.product_uom_id,
                sml.location_id AS src_location_id,
                src.usage AS src_usage,
                sml.location_dest_id AS dst_location_id,
                dst.usage AS dst_usage
            FROM stock_move_line sml
            JOIN stock_location src ON src.id = sml.location_id
            JOIN stock_location dst ON dst.id = sml.location_dest_id
            WHERE sml.state = 'done'
              AND sml.date <= %s
              AND sml.product_id = ANY(%s)
              AND (src.usage = 'internal' OR dst.usage = 'internal')
            ORDER BY sml.date, sml.id
        """, [date_to, list(products.ids)])
        lines = self.env.cr.dictfetchall()
        if not lines:
            return {}

        move_ids = sorted({l["move_id"] for l in lines if l.get("move_id")})
        unit_cost_by_move = {}
        if move_ids:
            self.env.cr.execute("""
                SELECT
                    stock_move_id,
                    COALESCE(SUM(value), 0) AS val,
                    COALESCE(SUM(quantity), 0) AS qty
                FROM stock_valuation_layer
                WHERE stock_move_id = ANY(%s)
                GROUP BY stock_move_id
            """, [move_ids])
            for r in self.env.cr.dictfetchall():
                qty = r["qty"] or 0.0
                unit_cost_by_move[r["stock_move_id"]] = (r["val"] / qty) if qty else 0.0

        uom_ids = sorted({l["product_uom_id"] for l in lines if l.get("product_uom_id")})
        uoms = self.env["uom.uom"].browse(uom_ids).exists()
        uom_map = {u.id: u for u in uoms}

        layers = defaultdict(lambda: defaultdict(deque))

        def _add_layer(location_id, product_id, qty, unit_cost):
            if not location_id or qty <= 0:
                return
            layers[location_id][product_id].append([qty, unit_cost])

        def _consume(location_id, product_id, qty):
            if not location_id or qty <= 0:
                return
            dq = layers[location_id][product_id]
            remain = qty
            while remain > 0 and dq:
                lqty, cost = dq[0]
                take = lqty if lqty <= remain else remain
                lqty -= take
                remain -= take
                if lqty <= 0:
                    dq.popleft()
                else:
                    dq[0][0] = lqty

        def _transfer(src_location_id, dst_location_id, product_id, qty):
            if not src_location_id or not dst_location_id or qty <= 0:
                return
            dq = layers[src_location_id][product_id]
            remain = qty
            while remain > 0:
                if not dq:
                    _add_layer(dst_location_id, product_id, remain, 0.0)
                    break
                lqty, cost = dq[0]
                take = lqty if lqty <= remain else remain
                _add_layer(dst_location_id, product_id, take, cost)
                lqty -= take
                remain -= take
                if lqty <= 0:
                    dq.popleft()
                else:
                    dq[0][0] = lqty

        for l in lines:
            pid = l["product_id"]
            if pid not in product_uom_map:
                continue

            qty = l["qty_done"] or 0.0
            if qty <= 0:
                continue

            line_uom = uom_map.get(l.get("product_uom_id"))
            prod_uom = product_uom_map.get(pid)
            if line_uom and prod_uom and line_uom.id != prod_uom.id:
                qty = line_uom._compute_quantity(qty, prod_uom)

            src_internal = l.get("src_usage") == "internal"
            dst_internal = l.get("dst_usage") == "internal"
            src_loc = l.get("src_location_id")
            dst_loc = l.get("dst_location_id")

            if (not src_internal) and dst_internal:
                unit_cost = unit_cost_by_move.get(l.get("move_id"), 0.0)
                _add_layer(dst_loc, pid, qty, unit_cost)
            elif src_internal and (not dst_internal):
                _consume(src_loc, pid, qty)
            elif src_internal and dst_internal:
                _transfer(src_loc, dst_loc, pid, qty)

        unit_cost_by_loc_prod = {}
        for loc_id, prod_map in layers.items():
            for pid, dq in prod_map.items():
                total_qty = 0.0
                total_val = 0.0
                for q, c in dq:
                    total_qty += q
                    total_val += q * c
                unit_cost_by_loc_prod[(loc_id, pid)] = (total_val / total_qty) if total_qty else 0.0

        return unit_cost_by_loc_prod

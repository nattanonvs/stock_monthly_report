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

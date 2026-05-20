import logging
from datetime import date

from odoo import models

_logger = logging.getLogger(__name__)

class StockReport(models.Model):
    _inherit = "stock.quant"

    def _monthly_stock_report(self):
        ICP = self.env["ir.config_parameter"].sudo()

        months_back = int(ICP.get_param("stock_monthly_report.months_back") or 1)
        recipients = ICP.get_param("stock_monthly_report.recipients") or ""

        warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
        if warehouse and warehouse.lot_stock_id:
            location = warehouse.lot_stock_id
        else:
            location = self.env["stock.location"].search([("usage", "=", "internal")], limit=1)

        if not location:
            _logger.error("Monthly Stock Report cron: no internal location found.")
            return False

        today = date.today()
        end_month = today.month - 1
        end_year = today.year
        if end_month == 0:
            end_month = 12
            end_year -= 1

        start_month = max(1, end_month - max(1, months_back) + 1)

        wizard = self.env["stock.monthly.wizard"].create({
            "year": str(end_year),
            "month_start": str(start_month),
            "month_end": str(end_month),
            "location_ids": [(6, 0, [location.id])],
            "include_sub_locations": True,
            "include_zero_qty": False,
            "show_movements": True,
            "summary_only": False,
            "include_pdf": True,
            "include_excel": True,
            "send_email": True,
            "email_to": recipients,
        })

        try:
            wizard.action_generate_reports()
        except Exception:
            _logger.exception("Monthly Stock Report cron: failed to generate/send reports.")
            return False

        return True

from odoo import models, fields

class StockMonthlySettings(models.TransientModel):
    _name = "stock.monthly.settings"
    _inherit = "res.config.settings"
    _description = "Monthly Stock Report Settings"

    recipients = fields.Char(string="Recipients")
    valuation_policy = fields.Selection([
        ("fifo", "FIFO"),
        ("avco", "Average Cost"),
    ], string="Valuation Policy", default="fifo")
    months_back = fields.Integer(string="Months Back", default=1)
    timezone = fields.Char(string="Timezone", default="Asia/Bangkok")

    def set_values(self):
        super().set_values()
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("stock_monthly_report.recipients", self.recipients or "")
        ICP.set_param("stock_monthly_report.valuation_policy", self.valuation_policy or "fifo")
        ICP.set_param("stock_monthly_report.months_back", str(self.months_back or 1))
        ICP.set_param("stock_monthly_report.timezone", self.timezone or "Asia/Bangkok")

    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        res.update(
            recipients=ICP.get_param("stock_monthly_report.recipients") or "",
            valuation_policy=ICP.get_param("stock_monthly_report.valuation_policy") or "fifo",
            months_back=int(ICP.get_param("stock_monthly_report.months_back") or 1),
            timezone=ICP.get_param("stock_monthly_report.timezone") or "Asia/Bangkok",
        )
        return res
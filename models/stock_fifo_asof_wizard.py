# -*- coding: utf-8 -*-
import base64
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockFifoAsofWizard(models.TransientModel):
    _name = "stock.fifo.asof.wizard"
    _description = "FIFO Valuation As-of Date Wizard"

    asof_datetime = fields.Datetime(
        string="As of Date/Time",
        required=True,
        default=lambda self: fields.Datetime.now(),
        help="Inventory valuation as of this datetime (backdated)."
    )

    location_id = fields.Many2one(
        "stock.location",
        string="Location",
        domain=[("usage", "=", "internal")],
        required=True
    )
    include_sub_locations = fields.Boolean(
        string="Include Sub-locations",
        default=True
    )

    categ_id = fields.Many2one("product.category", string="Product Category")
    product_ids = fields.Many2many("product.product", string="Products")

    summary_only = fields.Boolean(
        string="Summary Only",
        default=False,
        help="Show only totals per Category (and Grand Total)."
    )

    include_excel = fields.Boolean(string="Include Excel", default=True)

    @api.onchange("categ_id")
    def _onchange_categ_id(self):
        if self.categ_id and self.product_ids:
            allowed_products = self.env["product.product"].search([
                ("categ_id", "child_of", self.categ_id.id)
            ])
            self.product_ids = self.product_ids & allowed_products

    def _get_location_ids(self):
        self.ensure_one()
        if self.include_sub_locations:
            return self.env["stock.location"].search([("id", "child_of", self.location_id.id)]).ids
        return [self.location_id.id]

    def action_generate_asof_xlsx(self):
        self.ensure_one()

        if not self.include_excel:
            raise UserError(_("Please select Excel output."))

        xlsx_action = self.env.ref(
            "stock_monthly_report.action_stock_fifo_asof_xlsx",
            raise_if_not_found=False
        )
        if not xlsx_action:
            raise UserError(_("Missing XLSX report action: stock_monthly_report.action_stock_fifo_asof_xlsx"))

        data = {
            "wizard_id": self.id,
        }

        # ทำเป็น download ตรง (แยกจากรายงานเดิม)
        return xlsx_action.report_action([], data=data)
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

    location_ids = fields.Many2many(
        "stock.location",
        string="Locations",
        domain=[("usage", "=", "internal")],
        required=True
    )
    include_sub_locations = fields.Boolean(
        string="Include Sub-locations",
        default=True
    )

    include_categ_ids = fields.Many2many(
        "product.category",
        "stock_fifo_asof_wizard_include_categ_rel",
        "wizard_id",
        "categ_id",
        string="Include Product Categories",
    )
    exclude_categ_ids = fields.Many2many(
        "product.category",
        "stock_fifo_asof_wizard_exclude_categ_rel",
        "wizard_id",
        "categ_id",
        string="Exclude Product Categories",
    )
    product_ids = fields.Many2many("product.product", string="Products")

    summary_only = fields.Boolean(
        string="Summary Only",
        default=False,
        help="Show only totals per Category (and Grand Total)."
    )

    include_excel = fields.Boolean(string="Include Excel", default=True)

    def _get_category_filter_sets(self):
        self.ensure_one()
        include_subtree_ids = set()
        if self.include_categ_ids:
            include_subtree_ids = set(self.env["product.category"].search([
                ("id", "child_of", self.include_categ_ids.ids)
            ]).ids)

        exclude_subtree_ids = set()
        if self.exclude_categ_ids:
            exclude_subtree_ids = set(self.env["product.category"].search([
                ("id", "child_of", self.exclude_categ_ids.ids)
            ]).ids)

        if include_subtree_ids:
            include_subtree_ids -= exclude_subtree_ids

        return include_subtree_ids, exclude_subtree_ids

    @api.onchange("include_categ_ids", "exclude_categ_ids")
    def _onchange_category_filters(self):
        domain = []
        include_subtree_ids, exclude_subtree_ids = self._get_category_filter_sets()
        if include_subtree_ids:
            domain.append(("categ_id", "in", list(include_subtree_ids)))
        if exclude_subtree_ids:
            domain.append(("categ_id", "not in", list(exclude_subtree_ids)))

        if domain and self.product_ids:
            allowed_products = self.env["product.product"].search(domain)
            self.product_ids = self.product_ids & allowed_products

        return {"domain": {"product_ids": domain}}

    def _get_location_ids(self):
        self.ensure_one()
        if self.include_sub_locations:
            return self.env["stock.location"].search([
                ("id", "child_of", self.location_ids.ids),
                ("usage", "=", "internal"),
            ]).ids
        return self.location_ids.ids

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

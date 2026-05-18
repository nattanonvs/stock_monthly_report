# -*- coding: utf-8 -*-
{
    "name": "Monthly Stock Report",
    "version": "15.0.1.0.1",
    "summary": "Monthly Stock On Hand via PDF & Excel, FIFO valuation, UI menus, wizard, and email automation",
    "author": "Nattanon",
    "depends": ["stock", "mail", "report_xlsx"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_config_parameter.xml",
        "data/scheduled_action.xml",
        "data/report_xlsx_action.xml",
        "views/stock_monthly_settings_view.xml",
        "views/stock_monthly_wizard_view.xml",
        "views/stock_fifo_asof_wizard_view.xml",
        "data/menu_report.xml",
        "report/stock_report_pdf.xml",
        "data/report_fifo_asof_action.xml",

    ],
    "license": "LGPL-3",
    "application": False,
    "installable": True,
}

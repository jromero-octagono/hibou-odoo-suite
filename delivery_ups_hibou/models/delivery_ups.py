from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.delivery_ups.models.ups_request import UPSRequest, Package
from odoo.tools import pdf


class ProviderUPS(models.Model):
    _inherit = 'delivery.carrier'

    def _get_ups_is_third_party(self, order=None, picking=None):
        third_party_account = self.get_third_party_account(order=order, picking=picking)
        if third_party_account:
            if not third_party_account.delivery_type == 'ups':
                raise ValidationError('Non-UPS Shipping Account indicated during UPS shipment.')
            return True
        if order and self.ups_bill_my_account and order.ups_carrier_account:
            return True
        return False

    def _get_ups_account_number(self, order=None, picking=None):
        """
        Common hook to customize what UPS Account number to use.
        :return: UPS Account Number
        """
        # Provided by Hibou Odoo Suite `delivery_hibou`
        third_party_account = self.get_third_party_account(order=order, picking=picking)
        if third_party_account:
            if not third_party_account.delivery_type == 'ups':
                raise ValidationError('Non-UPS Shipping Account indicated during UPS shipment.')
            return third_party_account.name
        if order and order.ups_carrier_account:
            return order.ups_carrier_account
        if picking and picking.sale_id.ups_carrier_account:
            return picking.sale_id.ups_carrier_account
        return self.ups_shipper_number

    def _get_ups_carrier_account(self, picking):
        # 3rd party billing should return False if not used.
        account = self._get_ups_account_number(picking=picking)
        return account if account != self.ups_shipper_number else False

    """
    Overrides to use Hibou Delivery methods to get shipper etc. and to add 'transit_days' to result.
    """
    def ups_rate_shipment(self, order):
        superself = self.sudo()
        srm = UPSRequest(self.log_xml, superself.ups_username, superself.ups_passwd, superself.ups_shipper_number, superself.ups_access_number, self.prod_environment)
        ResCurrency = self.env['res.currency']
        max_weight = self.ups_default_packaging_id.max_weight
        packages = []
        total_qty = 0
        total_weight = 0
        for line in order.order_line.filtered(lambda line: not line.is_delivery):
            total_qty += line.product_uom_qty
            total_weight += line.product_id.weight * line.product_qty

        if max_weight and total_weight > max_weight:
            total_package = int(total_weight / max_weight)
            last_package_weight = total_weight % max_weight

            for seq in range(total_package):
                packages.append(Package(self, max_weight))
            if last_package_weight:
                packages.append(Package(self, last_package_weight))
        else:
            packages.append(Package(self, total_weight))

        shipment_info = {
            'total_qty': total_qty  # required when service type = 'UPS Worldwide Express Freight'
        }

        if self.ups_cod:
            cod_info = {
                'currency': order.partner_id.country_id.currency_id.name,
                'monetary_value': order.amount_total,
                'funds_code': self.ups_cod_funds_code,
            }
        else:
            cod_info = None

        # Hibou Delivery
        shipper_company = self.get_shipper_company(order=order)
        shipper_warehouse = self.get_shipper_warehouse(order=order)
        recipient = self.get_recipient(order=order)
        date_planned = None
        if self.env.context.get('date_planned'):
            date_planned = self.env.context.get('date_planned')

        check_value = srm.check_required_value(shipper_company, shipper_warehouse, recipient, order=order)
        if check_value:
            return {'success': False,
                    'price': 0.0,
                    'error_message': check_value,
                    'warning_message': False}

        ups_service_type = order.ups_service_type or self.ups_default_service_type
        result = srm.get_shipping_price(
            shipment_info=shipment_info, packages=packages, shipper=shipper_company, ship_from=shipper_warehouse,
            ship_to=recipient, packaging_type=self.ups_default_packaging_id.shipper_package_code, service_type=ups_service_type,
            saturday_delivery=self.ups_saturday_delivery, cod_info=cod_info, date_planned=date_planned)

        if result.get('error_message'):
            return {'success': False,
                    'price': 0.0,
                    'error_message': _('Error:\n%s') % result['error_message'],
                    'warning_message': False}

        if order.currency_id.name == result['currency_code']:
            price = float(result['price'])
        else:
            quote_currency = ResCurrency.search([('name', '=', result['currency_code'])], limit=1)
            price = quote_currency._convert(
                float(result['price']), order.currency_id, order.company_id, order.date_order or fields.Date.today())

        # Hibou Delivery
        if self._get_ups_is_third_party(order=order):
            # Don't show delivery amount, if ups bill my account option is true
            price = 0.0

        return {'success': True,
                'price': price,
                'transit_days': result.get('transit_days', 0),
                'error_message': False,
                'warning_message': False}

    def ups_send_shipping(self, pickings):
        res = []
        superself = self.sudo()
        srm = UPSRequest(self.log_xml, superself.ups_username, superself.ups_passwd, superself.ups_shipper_number, superself.ups_access_number, self.prod_environment)
        ResCurrency = self.env['res.currency']
        for picking in pickings:
            # Hibou Delivery
            shipper_company = superself.get_shipper_company(picking=picking)
            shipper_warehouse = superself.get_shipper_warehouse(picking=picking)
            recipient = superself.get_recipient(picking=picking)

            packages = []
            package_names = []
            if picking.package_ids:
                # Create all packages
                for package in picking.package_ids:
                    packages.append(Package(self, package.shipping_weight, quant_pack=package.packaging_id, name=package.name))
                    package_names.append(package.name)
            # Create one package with the rest (the content that is not in a package)
            if picking.weight_bulk:
                packages.append(Package(self, picking.weight_bulk))

            invoice_line_total = 0
            for move in picking.move_lines:
                invoice_line_total += picking.company_id.currency_id.round(move.product_id.lst_price * move.product_qty)

            shipment_info = {
                'description': superself.get_order_name(picking=picking),
                'total_qty': sum(sml.qty_done for sml in picking.move_line_ids),
                'ilt_monetary_value': '%d' % invoice_line_total,
                'itl_currency_code': self.env.user.company_id.currency_id.name,
                'phone': recipient.mobile or recipient.phone,
            }
            if picking.sale_id and picking.sale_id.carrier_id != picking.carrier_id:
                ups_service_type = picking.carrier_id.ups_default_service_type or picking.ups_service_type or superself.ups_default_service_type
            else:
                ups_service_type = picking.ups_service_type or superself.ups_default_service_type

            # Hibou Delivery
            ups_carrier_account = superself._get_ups_carrier_account(picking)

            if picking.carrier_id.ups_cod:
                cod_info = {
                    'currency': picking.partner_id.country_id.currency_id.name,
                    'monetary_value': picking.sale_id.amount_total,
                    'funds_code': superself.ups_cod_funds_code,
                }
            else:
                cod_info = None

            check_value = srm.check_required_value(shipper_company, shipper_warehouse, recipient, picking=picking)
            if check_value:
                raise UserError(check_value)

            package_type = picking.package_ids and picking.package_ids[0].packaging_id.shipper_package_code or self.ups_default_packaging_id.shipper_package_code
            result = srm.send_shipping(
                shipment_info=shipment_info, packages=packages, shipper=shipper_company, ship_from=shipper_warehouse,
                ship_to=recipient, packaging_type=package_type, service_type=ups_service_type, label_file_type=self.ups_label_file_type, ups_carrier_account=ups_carrier_account,
                saturday_delivery=picking.carrier_id.ups_saturday_delivery, cod_info=cod_info)
            if result.get('error_message'):
                raise UserError(result['error_message'])

            order = picking.sale_id
            company = order.company_id or picking.company_id or self.env.user.company_id
            currency_order = picking.sale_id.currency_id
            if not currency_order:
                currency_order = picking.company_id.currency_id

            if currency_order.name == result['currency_code']:
                price = float(result['price'])
            else:
                quote_currency = ResCurrency.search([('name', '=', result['currency_code'])], limit=1)
                price = quote_currency._convert(
                    float(result['price']), currency_order, company, order.date_order or fields.Date.today())

            package_labels = []
            for track_number, label_binary_data in result.get('label_binary_data').items():
                package_labels = package_labels + [(track_number, label_binary_data)]

            carrier_tracking_ref = "+".join([pl[0] for pl in package_labels])
            logmessage = _("Shipment created into UPS<br/>"
                           "<b>Tracking Numbers:</b> %s<br/>"
                           "<b>Packages:</b> %s") % (carrier_tracking_ref, ','.join(package_names))
            if superself.ups_label_file_type != 'GIF':
                attachments = [('LabelUPS-%s.%s' % (pl[0], superself.ups_label_file_type), pl[1]) for pl in package_labels]
            if superself.ups_label_file_type == 'GIF':
                attachments = [('LabelUPS.pdf', pdf.merge_pdf([pl[1] for pl in package_labels]))]
            picking.message_post(body=logmessage, attachments=attachments)
            shipping_data = {
                'exact_price': price,
                'tracking_number': carrier_tracking_ref}
            res = res + [shipping_data]
        return res

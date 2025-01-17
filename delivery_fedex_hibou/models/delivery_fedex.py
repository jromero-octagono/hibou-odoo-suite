import logging
import time
from odoo import fields, models, tools, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.delivery_fedex.models.delivery_fedex import _convert_curr_iso_fdx, _convert_weight
from .fedex_request import FedexRequest

pdf = tools.pdf
_logger = logging.getLogger(__name__)


class DeliveryFedex(models.Model):
    _inherit = 'delivery.carrier'

    fedex_service_type = fields.Selection(selection_add=[
        ('GROUND_HOME_DELIVERY', 'GROUND_HOME_DELIVERY'),
        ('FEDEX_EXPRESS_SAVER', 'FEDEX_EXPRESS_SAVER'),
    ])

    def _get_fedex_is_third_party(self, order=None, picking=None):
        third_party_account = self.get_third_party_account(order=order, picking=picking)
        if third_party_account:
            if not third_party_account.delivery_type == 'fedex':
                raise ValidationError('Non-FedEx Shipping Account indicated during FedEx shipment.')
            return True
        return False

    def _get_fedex_payment_account_number(self, order=None, picking=None):
        """
        Common hook to customize what Fedex Account number to use.
        :return: FedEx Account Number
        """
        # Provided by Hibou Odoo Suite `delivery_hibou`
        third_party_account = self.get_third_party_account(order=order, picking=picking)
        if third_party_account:
            if not third_party_account.delivery_type == 'fedex':
                raise ValidationError('Non-FedEx Shipping Account indicated during FedEx shipment.')
            return third_party_account.name
        return self.fedex_account_number

    def _get_fedex_account_number(self, order=None, picking=None):
        if order:
            # third_party_account = self.get_third_party_account(order=order, picking=picking)
            # if third_party_account:
            #     if not third_party_account.delivery_type == 'fedex':
            #         raise ValidationError('Non-FedEx Shipping Account indicated during FedEx shipment.')
            #     return third_party_account.name
            if order.warehouse_id.fedex_account_number:
                return order.warehouse_id.fedex_account_number
            return self.fedex_account_number
        if picking:
            if picking.picking_type_id.warehouse_id.fedex_account_number:
                return picking.picking_type_id.warehouse_id.fedex_account_number
        return self.fedex_account_number

    def _get_fedex_meter_number(self, order=None, picking=None):
        if order:
            if order.warehouse_id.fedex_meter_number:
                return order.warehouse_id.fedex_meter_number
            return self.fedex_meter_number
        if picking:
            if picking.picking_type_id.warehouse_id.fedex_meter_number:
                return picking.picking_type_id.warehouse_id.fedex_meter_number
        return self.fedex_meter_number

    def _get_fedex_recipient_is_residential(self, partner):
        if self.fedex_service_type.find('HOME'):
            return True
        return not bool(partner.company)

    def _fedex_srm_shipment_label(self, srm, label_format_type, image_type, label_stock_type, label_printing_orientation, label_order):
        """
        This gets called when the shipment label details are being created.
        Override this method, add new parameters if needed.  Do not call super if you cannot curry your own parameters.
        """
        srm.shipment_label(label_format_type, image_type, label_stock_type, label_printing_orientation, label_order)

    ##
    # Main Overrides
    #
    # Note that a lot of these comments are from the overridden code, they are left here
    # for easier diffing against Odoo Enterprise

    """
    Overrides to use Hibou Delivery methods to get shipper etc. and to add 'transit_days' to result.
    """
    def fedex_rate_shipment(self, order):
        max_weight = _convert_weight(self.fedex_default_packaging_id.max_weight, self.fedex_weight_unit)
        price = 0.0
        is_india = order.partner_shipping_id.country_id.code == 'IN' and order.company_id.partner_id.country_id.code == 'IN'

        # Estimate weight of the sales order; will be definitely recomputed on the picking field "weight"
        est_weight_value = sum([(line.product_id.weight * line.product_uom_qty) for line in order.order_line]) or 0.0
        weight_value = _convert_weight(est_weight_value, self.fedex_weight_unit)

        # Some users may want to ship very lightweight items; in order to give them a rating, we round the
        # converted weight of the shipping to the smallest value accepted by FedEx: 0.01 kg or lb.
        # (in the case where the weight is actually 0.0 because weights are not set, don't do this)
        if weight_value > 0.0:
            weight_value = max(weight_value, 0.01)

        order_currency = order.currency_id
        superself = self.sudo()

        shipper_company = superself.get_shipper_company(order=order)
        shipper_warehouse = superself.get_shipper_warehouse(order=order)
        recipient = superself.get_recipient(order=order)
        acc_number = superself._get_fedex_account_number(order=order)
        order_name = superself.get_order_name(order=order)
        residential = self._get_fedex_recipient_is_residential(recipient)

        date_planned = None
        if self.env.context.get('date_planned'):
            date_planned = self.env.context.get('date_planned')


        # Authentication stuff
        srm = FedexRequest(self.log_xml, request_type="rating", prod_environment=self.prod_environment)
        srm.web_authentication_detail(superself.fedex_developer_key, superself.fedex_developer_password)
        srm.client_detail(acc_number, superself.fedex_meter_number)

        # Build basic rating request and set addresses
        srm.transaction_detail(order_name)
        srm.shipment_request(
            self.fedex_droppoff_type,
            self.fedex_service_type,
            self.fedex_default_packaging_id.shipper_package_code,
            self.fedex_weight_unit,
            self.fedex_saturday_delivery,
        )

        srm.set_currency(_convert_curr_iso_fdx(order_currency.name))
        srm.set_shipper(shipper_company, shipper_warehouse)
        srm.set_recipient(recipient, residential=residential)

        if max_weight and weight_value > max_weight:
            total_package = int(weight_value / max_weight)
            last_package_weight = weight_value % max_weight

            for sequence in range(1, total_package + 1):
                srm.add_package(max_weight, sequence_number=sequence, mode='rating')
            if last_package_weight:
                total_package = total_package + 1
                srm.add_package(last_package_weight, sequence_number=total_package, mode='rating')
            srm.set_master_package(weight_value, total_package)
        else:
            srm.add_package(weight_value, mode='rating')
            srm.set_master_package(weight_value, 1)

        # Commodities for customs declaration (international shipping)
        if self.fedex_service_type in ['INTERNATIONAL_ECONOMY', 'INTERNATIONAL_PRIORITY'] or is_india:
            total_commodities_amount = 0.0
            commodity_country_of_manufacture = order.warehouse_id.partner_id.country_id.code

            for line in order.order_line.filtered(lambda l: l.product_id.type in ['product', 'consu']):
                commodity_amount = line.price_total / line.product_uom_qty
                total_commodities_amount += (commodity_amount * line.product_uom_qty)
                commodity_description = line.product_id.name
                commodity_number_of_piece = '1'
                commodity_weight_units = self.fedex_weight_unit
                commodity_weight_value = _convert_weight(line.product_id.weight * line.product_uom_qty, self.fedex_weight_unit)
                commodity_quantity = line.product_uom_qty
                commodity_quantity_units = 'EA'
                commodity_harmonized_code = line.product_id.hs_code or ''
                srm._commodities(_convert_curr_iso_fdx(order_currency.name), commodity_amount, commodity_number_of_piece, commodity_weight_units, commodity_weight_value, commodity_description, commodity_country_of_manufacture, commodity_quantity, commodity_quantity_units, commodity_harmonized_code)
            srm.customs_value(_convert_curr_iso_fdx(order_currency.name), total_commodities_amount, "NON_DOCUMENTS")
            srm.duties_payment(order.warehouse_id.partner_id.country_id.code, superself.fedex_account_number)

        request = srm.rate(date_planned=date_planned)

        warnings = request.get('warnings_message')
        if warnings:
            _logger.info(warnings)

        if not request.get('errors_message'):
            if _convert_curr_iso_fdx(order_currency.name) in request['price']:
                price = request['price'][_convert_curr_iso_fdx(order_currency.name)]
            else:
                _logger.info("Preferred currency has not been found in FedEx response")
                company_currency = order.company_id.currency_id
                if _convert_curr_iso_fdx(company_currency.name) in request['price']:
                    price = company_currency.compute(request['price'][_convert_curr_iso_fdx(company_currency.name)], order_currency)
                else:
                    price = company_currency.compute(request['price']['USD'], order_currency)
        else:
            return {'success': False,
                    'price': 0.0,
                    'error_message': _('Error:\n%s') % request['errors_message'],
                    'warning_message': False}

        return {'success': True,
                'price': float(price),
                'error_message': False,
                'transit_days': request.get('transit_days', False),
                'date_delivered': request.get('date_delivered', False),
                'warning_message': _('Warning:\n%s') % warnings if warnings else False}

    """
    Overrides to use Hibou Delivery methods to get shipper etc. and add insurance.
    """
    def fedex_send_shipping(self, pickings):
        res = []

        for picking in pickings:
            srm = FedexRequest(self.log_xml, request_type="shipping", prod_environment=self.prod_environment)
            superself = self.sudo()

            shipper_company = superself.get_shipper_company(picking=picking)
            shipper_warehouse = superself.get_shipper_warehouse(picking=picking)
            recipient = superself.get_recipient(picking=picking)
            acc_number = superself._get_fedex_account_number(picking=picking)
            meter_number = superself._get_fedex_meter_number(picking=picking)
            payment_acc_number = superself._get_fedex_payment_account_number()
            order_name = superself.get_order_name(picking=picking)
            attn = superself.get_attn(picking=picking)
            insurance_value = superself.get_insurance_value(picking=picking)
            residential = self._get_fedex_recipient_is_residential(recipient)

            srm.web_authentication_detail(superself.fedex_developer_key, superself.fedex_developer_password)
            srm.client_detail(acc_number, meter_number)

            # Not the actual reference.  Using `shipment_name` during `add_package` calls.
            srm.transaction_detail(picking.id)

            package_type = picking.package_ids and picking.package_ids[0].packaging_id.shipper_package_code or self.fedex_default_packaging_id.shipper_package_code
            srm.shipment_request(self.fedex_droppoff_type, self.fedex_service_type, package_type, self.fedex_weight_unit, self.fedex_saturday_delivery)
            srm.set_currency(_convert_curr_iso_fdx(picking.company_id.currency_id.name))
            srm.set_shipper(shipper_company, shipper_warehouse)
            srm.set_recipient(recipient, attn=attn, residential=residential)

            srm.shipping_charges_payment(payment_acc_number, third_party=bool(self.get_third_party_account(picking=picking)))

            # Commonly this needs to be modified, e.g. for doc tabs.  Do not want to have to patch this entire method.
            #srm.shipment_label('COMMON2D', self.fedex_label_file_type, self.fedex_label_stock_type, 'TOP_EDGE_OF_TEXT_FIRST', 'SHIPPING_LABEL_FIRST')
            self._fedex_srm_shipment_label(srm, 'COMMON2D', self.fedex_label_file_type, self.fedex_label_stock_type, 'TOP_EDGE_OF_TEXT_FIRST', 'SHIPPING_LABEL_FIRST')

            order_currency = picking.sale_id.currency_id or picking.company_id.currency_id

            net_weight = _convert_weight(picking.shipping_weight, self.fedex_weight_unit)

            # Commodities for customs declaration (international shipping)
            if self.fedex_service_type in ['INTERNATIONAL_ECONOMY', 'INTERNATIONAL_PRIORITY'] or (picking.partner_id.country_id.code == 'IN' and picking.picking_type_id.warehouse_id.partner_id.country_id.code == 'IN'):

                commodity_currency = order_currency
                total_commodities_amount = 0.0
                commodity_country_of_manufacture = picking.picking_type_id.warehouse_id.partner_id.country_id.code

                for operation in picking.move_line_ids:
                    commodity_amount = order_currency.compute(operation.product_id.list_price, commodity_currency)
                    total_commodities_amount += (commodity_amount * operation.qty_done)
                    commodity_description = operation.product_id.name
                    commodity_number_of_piece = '1'
                    commodity_weight_units = self.fedex_weight_unit
                    commodity_weight_value = _convert_weight(operation.product_id.weight * operation.qty_done, self.fedex_weight_unit)
                    commodity_quantity = operation.qty_done
                    commodity_quantity_units = 'EA'
                    srm.commodities(_convert_curr_iso_fdx(commodity_currency.name), commodity_amount, commodity_number_of_piece, commodity_weight_units, commodity_weight_value, commodity_description, commodity_country_of_manufacture, commodity_quantity, commodity_quantity_units)
                srm.customs_value(_convert_curr_iso_fdx(commodity_currency.name), total_commodities_amount, "NON_DOCUMENTS")
                srm.duties_payment(picking.picking_type_id.warehouse_id.partner_id.country_id.code, superself.fedex_account_number)

            package_count = len(picking.package_ids) or 1

            # TODO RIM master: factorize the following crap

            ################
            # Multipackage #
            ################
            if package_count > 1:

                # Note: Fedex has a complex multi-piece shipping interface
                # - Each package has to be sent in a separate request
                # - First package is called "master" package and holds shipping-
                #   related information, including addresses, customs...
                # - Last package responses contains shipping price and code
                # - If a problem happens with a package, every previous package
                #   of the shipping has to be cancelled separately
                # (Why doing it in a simple way when the complex way exists??)

                master_tracking_id = False
                package_labels = []
                carrier_tracking_ref = ""

                for sequence, package in enumerate(picking.package_ids, start=1):

                    package_weight = _convert_weight(package.shipping_weight, self.fedex_weight_unit)

                    # Hibou Delivery
                    # Add more details to package.
                    srm.add_package(package_weight, sequence_number=sequence, ref=('%s-%d' % (order_name, sequence)), insurance=insurance_value)
                    srm.set_master_package(net_weight, package_count, master_tracking_id=master_tracking_id)
                    request = srm.process_shipment()
                    package_name = package.name or sequence

                    warnings = request.get('warnings_message')
                    if warnings:
                        _logger.info(warnings)

                    # First package
                    if sequence == 1:
                        if not request.get('errors_message'):
                            master_tracking_id = request['master_tracking_id']
                            package_labels.append((package_name, srm.get_label()))
                            carrier_tracking_ref = request['tracking_number']
                        else:
                            raise UserError(request['errors_message'])

                    # Intermediary packages
                    elif sequence > 1 and sequence < package_count:
                        if not request.get('errors_message'):
                            package_labels.append((package_name, srm.get_label()))
                            carrier_tracking_ref = carrier_tracking_ref + "," + request['tracking_number']
                        else:
                            raise UserError(request['errors_message'])

                    # Last package
                    elif sequence == package_count:
                        # recuperer le label pdf
                        if not request.get('errors_message'):
                            package_labels.append((package_name, srm.get_label()))

                            if _convert_curr_iso_fdx(order_currency.name) in request['price']:
                                carrier_price = request['price'][_convert_curr_iso_fdx(order_currency.name)]
                            else:
                                _logger.info("Preferred currency has not been found in FedEx response")
                                company_currency = picking.company_id.currency_id
                                if _convert_curr_iso_fdx(company_currency.name) in request['price']:
                                    carrier_price = company_currency.compute(request['price'][_convert_curr_iso_fdx(company_currency.name)], order_currency)
                                else:
                                    carrier_price = company_currency.compute(request['price']['USD'], order_currency)

                            carrier_tracking_ref = carrier_tracking_ref + "," + request['tracking_number']

                            logmessage = _("Shipment created into Fedex<br/>"
                                           "<b>Tracking Numbers:</b> %s<br/>"
                                           "<b>Packages:</b> %s") % (carrier_tracking_ref, ','.join([pl[0] for pl in package_labels]))
                            if self.fedex_label_file_type != 'PDF':
                                attachments = [('LabelFedex-%s.%s' % (pl[0], self.fedex_label_file_type), pl[1]) for pl in package_labels]
                            if self.fedex_label_file_type == 'PDF':
                                attachments = [('LabelFedex.pdf', pdf.merge_pdf([pl[1] for pl in package_labels]))]
                            picking.message_post(body=logmessage, attachments=attachments)
                            shipping_data = {'exact_price': carrier_price,
                                             'tracking_number': carrier_tracking_ref}
                            res = res + [shipping_data]
                        else:
                            raise UserError(request['errors_message'])

            # TODO RIM handle if a package is not accepted (others should be deleted)

            ###############
            # One package #
            ###############
            elif package_count == 1:
                # Hibou Delivery
                # Add more details to package.
                srm.add_package(net_weight, ref=order_name, insurance=insurance_value)
                srm.set_master_package(net_weight, 1)

                # Ask the shipping to fedex
                request = srm.process_shipment()

                warnings = request.get('warnings_message')
                if warnings:
                    _logger.info(warnings)

                if not request.get('errors_message'):

                    if _convert_curr_iso_fdx(order_currency.name) in request['price']:
                        carrier_price = request['price'][_convert_curr_iso_fdx(order_currency.name)]
                    else:
                        _logger.info("Preferred currency has not been found in FedEx response")
                        company_currency = picking.company_id.currency_id
                        if _convert_curr_iso_fdx(company_currency.name) in request['price']:
                            carrier_price = company_currency.compute(request['price'][_convert_curr_iso_fdx(company_currency.name)], order_currency)
                        else:
                            carrier_price = company_currency.compute(request['price']['USD'], order_currency)

                    carrier_tracking_ref = request['tracking_number']
                    logmessage = (_("Shipment created into Fedex <br/> <b>Tracking Number : </b>%s") % (carrier_tracking_ref))

                    fedex_labels = [('LabelFedex-%s-%s.%s' % (carrier_tracking_ref, index, self.fedex_label_file_type), label)
                                    for index, label in enumerate(srm._get_labels(self.fedex_label_file_type))]
                    picking.message_post(body=logmessage, attachments=fedex_labels)

                    shipping_data = {'exact_price': carrier_price,
                                     'tracking_number': carrier_tracking_ref}
                    res = res + [shipping_data]
                else:
                    raise UserError(request['errors_message'])

            ##############
            # No package #
            ##############
            else:
                raise UserError(_('No packages for this picking'))

        return res

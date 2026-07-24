from .forms import VehicleForm
from .models.sales import SaleType
from .models.vehicles import ArchivedVehicle, Vehicle


class AutomotiveSaleHandler:
    """Coordinates the typed automotive extension attached to a Sale."""

    detail_form_class = VehicleForm
    form_template = 'sale_details/_automotive_form.html'
    summary_template = 'sale_details/_automotive_summary.html'
    dialog_template = 'sale_details/_automotive_dialog.html'
    print_template = 'sale_details/_automotive_print.html'

    @classmethod
    def build_form(cls, *args, **kwargs):
        return cls.detail_form_class(*args, **kwargs)

    @staticmethod
    def save_details(form, sale):
        return form.save(sale)

    @staticmethod
    def archive_details(sale, archived_sale):
        try:
            vehicle = sale.vehicle
        except Vehicle.DoesNotExist:
            return None
        return ArchivedVehicle.objects.create(
            archived_sale=archived_sale,
            year=vehicle.year,
            make_name=vehicle.make.name,
            model_name=vehicle.model.name,
            mileage=vehicle.mileage,
            stock_number=vehicle.stock_number,
            vin=vehicle.vin,
        )


SALE_TYPE_HANDLERS = {
    SaleType.AUTOMOTIVE: AutomotiveSaleHandler,
}


def get_sale_type_handler(sale_type):
    try:
        return SALE_TYPE_HANDLERS[sale_type]
    except KeyError as exc:
        raise ValueError(f'Unsupported sale type: {sale_type}') from exc

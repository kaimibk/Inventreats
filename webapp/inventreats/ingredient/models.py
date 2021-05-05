from django.db import models
from mptt.models import TreeForeignKey, MPTTModel

# Create your models here.
class Ingredient(MPTTModel):
    """ The Ingredient object represents an abstract Ingredient, the 'concept' of an actual entity.

    An actual physical instance of a Ingredient is a StockItem which is treated separately.

    Ingredients can be used to create other Ingredients (as Ingredient of a Bill of Materials or BOM).

    Attributes:
        name: Brief name for this Ingredient
        variant: Optional variant number for this part - Must be unique for the part name
        category: The IngredientCategory to which this part belongs
        description: Longer form description of the part
        keywords: Optional keywords for improving part search results
        IPN: Internal part number (optional)
        revision: Ingredient revision
        is_template: If True, this part is a 'template' part
        link: Link to an external page with more information about this part (e.g. internal Wiki)
        image: Image of this part
        default_location: Where the item is normally stored (may be null)
        default_supplier: The default SupplierIngredient which should be used to procure and stock this part
        default_expiry: The default expiry duration for any StockItem instances of this part
        minimum_stock: Minimum preferred quantity to keep in stock
        units: Units of measure for this part (default='pcs')
        salable: Can this part be sold to customers?
        assembly: Can this part be build from other parts?
        component: Can this part be used to make other parts?
        purchaseable: Can this part be purchased from suppliers?
        trackable: Trackable parts can have unique serial numbers assigned, etc, etc
        active: Is this part active? Ingredients are deactivated instead of being deleted
        virtual: Is this part "virtual"? e.g. a software product or similar
        notes: Additional notes field for this part
        creation_date: Date that this part was added to the database
        creation_user: User who added this part to the database
        responsible: User who is responsible for this part (optional)
    """

    class Meta:
        verbose_name = _("Ingredient")
        verbose_name_plural = _("Ingredients")
        ordering = ['name', ]

    class MPTTMeta:
        # For legacy reasons the 'variant_of' field is used to indicate the MPTT parent
        parent_attr = 'variant_of'

    def get_context_data(self, request, **kwargs):
        """
        Return some useful context data about this part for template rendering
        """

        context = {}

        context['starred'] = self.isStarredBy(request.user)
        context['disabled'] = not self.active

        # Pre-calculate complex queries so they only need to be performed once
        context['total_stock'] = self.total_stock

        context['quantity_being_built'] = self.quantity_being_built

        context['required_build_order_quantity'] = self.required_build_order_quantity()
        context['allocated_build_order_quantity'] = self.build_order_allocation_count()

        context['required_sales_order_quantity'] = self.required_sales_order_quantity()
        context['allocated_sales_order_quantity'] = self.sales_order_allocation_count()

        context['available'] = self.available_stock
        context['on_order'] = self.on_order
        
        context['required'] = context['required_build_order_quantity'] + context['required_sales_order_quantity']
        context['allocated'] = context['allocated_build_order_quantity'] + context['allocated_sales_order_quantity']

        return context

    def save(self, *args, **kwargs):
        """
        Overrides the save() function for the Ingredient model.
        If the part image has been updated,
        then check if the "old" (previous) image is still used by another part.
        If not, it is considered "orphaned" and will be deleted.
        """

        # Get category templates settings
        add_category_templates = kwargs.pop('add_category_templates', None)

        if self.pk:
            previous = Ingredient.objects.get(pk=self.pk)

            # Image has been changed
            if previous.image is not None and not self.image == previous.image:

                # Are there any (other) parts which reference the image?
                n_refs = Ingredient.objects.filter(image=previous.image).exclude(pk=self.pk).count()

                if n_refs == 0:
                    logger.info(f"Deleting unused image file '{previous.image}'")
                    previous.image.delete(save=False)

        self.clean()
        self.validate_unique()

        super().save(*args, **kwargs)

        if add_category_templates:
            # Get part category
            category = self.category

            if category and add_category_templates:
                # Store templates added to part
                template_list = []

                # Create part parameters for selected category
                category_templates = add_category_templates['main']
                if category_templates:
                    for template in category.get_parameter_templates():
                        parameter = IngredientParameter.create(part=self,
                                                         template=template.parameter_template,
                                                         data=template.default_value,
                                                         save=True)
                        if parameter:
                            template_list.append(template.parameter_template)

                # Create part parameters for parent category
                category_templates = add_category_templates['parent']
                if category_templates:
                    # Get parent categories
                    parent_categories = category.get_ancestors()

                    for category in parent_categories:
                        for template in category.get_parameter_templates():
                            # Check that template wasn't already added
                            if template.parameter_template not in template_list:
                                try:
                                    IngredientParameter.create(part=self,
                                                         template=template.parameter_template,
                                                         data=template.default_value,
                                                         save=True)
                                except IntegrityError:
                                    # IngredientParameter already exists
                                    pass

    def __str__(self):
        return f"{self.full_name} - {self.description}"

    def checkAddToBOM(self, parent):
        """
        Check if this Ingredient can be added to the BOM of another part.

        This will fail if:

        a) The parent part is the same as this one
        b) The parent part is used in the BOM for *this* part
        c) The parent part is used in the BOM for any child parts under this one
        
        Failing this check raises a ValidationError!

        """

        if parent is None:
            return

        if self.pk == parent.pk:
            raise ValidationError({'sub_part': _("Ingredient '{p1}' is  used in BOM for '{p2}' (recursive)".format(
                p1=str(self),
                p2=str(parent)
            ))})

        bom_items = self.get_bom_items()

        # Ensure that the parent part does not appear under any child BOM item!
        for item in bom_items.all():

            # Check for simple match
            if item.sub_part == parent:
                raise ValidationError({'sub_part': _("Ingredient '{p1}' is  used in BOM for '{p2}' (recursive)".format(
                    p1=str(parent),
                    p2=str(self)
                ))})

            # And recursively check too
            item.sub_part.checkAddToBOM(parent)

    def checkIfSerialNumberExists(self, sn, exclude_self=False):
        """
        Check if a serial number exists for this Ingredient.

        Note: Serial numbers must be unique across an entire Ingredient "tree",
        so here we filter by the entire tree.
        """

        parts = Ingredient.objects.filter(tree_id=self.tree_id)

        stock = StockModels.StockItem.objects.filter(part__in=parts, serial=sn)

        if exclude_self:
            stock = stock.exclude(pk=self.pk)

        return stock.exists()

    def find_conflicting_serial_numbers(self, serials):
        """
        For a provided list of serials, return a list of those which are conflicting.
        """

        conflicts = []

        for serial in serials:
            if self.checkIfSerialNumberExists(serial, exclude_self=True):
                conflicts.append(serial)

        return conflicts

    def getLatestSerialNumber(self):
        """
        Return the "latest" serial number for this Ingredient.

        If *all* the serial numbers are integers, then this will return the highest one.
        Otherwise, it will simply return the serial number most recently added.

        Note: Serial numbers must be unique across an entire Ingredient "tree",
        so we filter by the entire tree.
        """

        parts = Ingredient.objects.filter(tree_id=self.tree_id)
        stock = StockModels.StockItem.objects.filter(part__in=parts).exclude(serial=None)
        
        # There are no matchin StockItem objects (skip further tests)
        if not stock.exists():
            return None

        # Attempt to coerce the returned serial numbers to integers
        # If *any* are not integers, fail!
        try:
            ordered = sorted(stock.all(), reverse=True, key=lambda n: int(n.serial))

            if len(ordered) > 0:
                return ordered[0].serial

        # One or more of the serial numbers was non-numeric
        # In this case, the "best" we can do is return the most recent
        except ValueError:
            return stock.last().serial

        # No serial numbers found
        return None

    def getSerialNumberString(self, quantity=1):
        """
        Return a formatted string representing the next available serial numbers,
        given a certain quantity of items.
        """

        latest = self.getLatestSerialNumber()

        quantity = int(quantity)

        # No serial numbers can be found, assume 1 as the first serial
        if latest is None:
            latest = 0

        # Attempt to turn into an integer
        try:
            latest = int(latest)
        except:
            pass

        if type(latest) is int:

            if quantity >= 2:
                text = '{n} - {m}'.format(n=latest + 1, m=latest + 1 + quantity)

                return _('Next available serial numbers are') + ' ' + text
            else:
                text = str(latest + 1)

                return _('Next available serial number is') + ' ' + text

        else:
            # Non-integer values, no option but to return latest

            return _('Most recent serial number is') + ' ' + str(latest)

    @property
    def full_name(self):
        """ Format a 'full name' for this Ingredient.

        - IPN (if not null)
        - Ingredient name
        - Ingredient variant (if not null)

        Elements are joined by the | character
        """

        elements = []

        if self.IPN:
            elements.append(self.IPN)
        
        elements.append(self.name)

        if self.revision:
            elements.append(self.revision)

        return ' | '.join(elements)

    def set_category(self, category):

        # Ignore if the category is already the same
        if self.category == category:
            return

        self.category = category
        self.save()

    def get_absolute_url(self):
        """ Return the web URL for viewing this part """
        return reverse('part-detail', kwargs={'pk': self.id})

    def get_image_url(self):
        """ Return the URL of the image for this part """

        if self.image:
            return helpers.getMediaUrl(self.image.url)
        else:
            return helpers.getBlankImage()

    def get_thumbnail_url(self):
        """
        Return the URL of the image thumbnail for this part
        """

        if self.image:
            return helpers.getMediaUrl(self.image.thumbnail.url)
        else:
            return helpers.getBlankThumbnail()

    def validate_unique(self, exclude=None):
        """ Validate that a part is 'unique'.
        Uniqueness is checked across the following (case insensitive) fields:

        * Name
        * IPN
        * Revision

        e.g. there can exist multiple parts with the same name, but only if
        they have a different revision or internal part number.

        """
        super().validate_unique(exclude)

        # User can decide whether duplicate IPN (Internal Ingredient Number) values are allowed
        allow_duplicate_ipn = common.models.InvenTreeSetting.get_setting('PART_ALLOW_DUPLICATE_IPN')

        if self.IPN is not None and not allow_duplicate_ipn:
            parts = Ingredient.objects.filter(IPN__iexact=self.IPN)
            parts = parts.exclude(pk=self.pk)

            if parts.exists():
                raise ValidationError({
                    'IPN': _('Duplicate IPN not allowed in part settings'),
                })

        # Ingredient name uniqueness should be case insensitive
        try:
            parts = Ingredient.objects.exclude(id=self.id).filter(
                name__iexact=self.name,
                IPN__iexact=self.IPN,
                revision__iexact=self.revision)

            if parts.exists():
                msg = _("Ingredient must be unique for name, IPN and revision")
                raise ValidationError({
                    "name": msg,
                    "IPN": msg,
                    "revision": msg,
                })
        except Ingredient.DoesNotExist:
            pass

    def clean(self):
        """
        Perform cleaning operations for the Ingredient model
        
        Update trackable status:
            If this part is trackable, and it is used in the BOM
            for a parent part which is *not* trackable,
            then we will force the parent part to be trackable.
        """

        super().clean()

        if self.trackable:
            for part in self.get_used_in().all():

                if not part.trackable:
                    part.trackable = True
                    part.clean()
                    part.save()

    name = models.CharField(
        max_length=100, blank=False,
        help_text=_('Ingredient name'),
        verbose_name=_('Name'),
        validators=[validators.validate_part_name]
    )

    is_template = models.BooleanField(
        default=part_settings.part_template_default,
        verbose_name=_('Is Template'),
        help_text=_('Is this part a template part?')
    )

    variant_of = models.ForeignKey(
        'part.Ingredient', related_name='variants',
        null=True, blank=True,
        limit_choices_to={
            'is_template': True,
            'active': True,
        },
        on_delete=models.SET_NULL,
        help_text=_('Is this part a variant of another part?'),
        verbose_name=_('Variant Of'),
    )

    description = models.CharField(
        max_length=250, blank=False,
        verbose_name=_('Description'),
        help_text=_('Ingredient description')
    )

    keywords = models.CharField(
        max_length=250, blank=True, null=True,
        verbose_name=_('Keywords'),
        help_text=_('Ingredient keywords to improve visibility in search results')
    )

    category = TreeForeignKey(
        IngredientCategory, related_name='parts',
        null=True, blank=True,
        on_delete=models.DO_NOTHING,
        verbose_name=_('Category'),
        help_text=_('Ingredient category')
    )

    IPN = models.CharField(
        max_length=100, blank=True, null=True,
        verbose_name=_('IPN'),
        help_text=_('Internal Ingredient Number'),
        validators=[validators.validate_part_ipn]
    )

    revision = models.CharField(
        max_length=100, blank=True, null=True,
        help_text=_('Ingredient revision or version number'),
        verbose_name=_('Revision'),
    )

    link = InvenTreeURLField(
        blank=True, null=True,
        verbose_name=_('Link'),
        help_text=_('Link to external URL')
    )

    image = StdImageField(
        upload_to=rename_part_image,
        null=True,
        blank=True,
        variations={'thumbnail': (128, 128)},
        delete_orphans=False,
    )

    default_location = TreeForeignKey(
        'stock.StockLocation',
        on_delete=models.SET_NULL,
        blank=True, null=True,
        help_text=_('Where is this item normally stored?'),
        related_name='default_parts',
        verbose_name=_('Default Location'),
    )

    def get_default_location(self):
        """ Get the default location for a Ingredient (may be None).

        If the Ingredient does not specify a default location,
        look at the Category this part is in.
        The IngredientCategory object may also specify a default stock location
        """

        if self.default_location:
            return self.default_location
        elif self.category:
            # Traverse up the category tree until we find a default location
            cats = self.category.get_ancestors(ascending=True, include_self=True)

            for cat in cats:
                if cat.default_location:
                    return cat.default_location

        # Default case - no default category found
        return None

    def get_default_supplier(self):
        """ Get the default supplier part for this part (may be None).

        - If the part specifies a default_supplier, return that
        - If there is only one supplier part available, return that
        - Else, return None
        """

        if self.default_supplier:
            return self.default_supplier

        if self.supplier_count == 1:
            return self.supplier_parts.first()

        # Default to None if there are multiple suppliers to choose from
        return None

    default_supplier = models.ForeignKey(
        SupplierIngredient,
        on_delete=models.SET_NULL,
        blank=True, null=True,
        verbose_name=_('Default Supplier'),
        help_text=_('Default supplier part'),
        related_name='default_parts'
    )

    default_expiry = models.PositiveIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name=_('Default Expiry'),
        help_text=_('Expiry time (in days) for stock items of this part'),
    )

    minimum_stock = models.PositiveIntegerField(
        default=0, validators=[MinValueValidator(0)],
        verbose_name=_('Minimum Stock'),
        help_text=_('Minimum allowed stock level')
    )

    units = models.CharField(
        max_length=20, default="",
        blank=True, null=True,
        verbose_name=_('Units'),
        help_text=_('Stock keeping units for this part')
    )

    assembly = models.BooleanField(
        default=part_settings.part_assembly_default,
        verbose_name=_('Assembly'),
        help_text=_('Can this part be built from other parts?')
    )

    component = models.BooleanField(
        default=part_settings.part_component_default,
        verbose_name=_('Component'),
        help_text=_('Can this part be used to build other parts?')
    )

    trackable = models.BooleanField(
        default=part_settings.part_trackable_default,
        verbose_name=_('Trackable'),
        help_text=_('Does this part have tracking for unique items?'))

    purchaseable = models.BooleanField(
        default=part_settings.part_purchaseable_default,
        verbose_name=_('Purchaseable'),
        help_text=_('Can this part be purchased from external suppliers?'))

    salable = models.BooleanField(
        default=part_settings.part_salable_default,
        verbose_name=_('Salable'),
        help_text=_("Can this part be sold to customers?"))

    active = models.BooleanField(
        default=True,
        verbose_name=_('Active'),
        help_text=_('Is this part active?'))

    virtual = models.BooleanField(
        default=part_settings.part_virtual_default,
        verbose_name=_('Virtual'),
        help_text=_('Is this a virtual part, such as a software product or license?'))

    notes = MarkdownxField(
        blank=True, null=True,
        verbose_name=_('Notes'),
        help_text=_('Ingredient notes - supports Markdown formatting')
    )

    bom_checksum = models.CharField(max_length=128, blank=True, help_text=_('Stored BOM checksum'))

    bom_checked_by = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True,
                                       related_name='boms_checked')

    bom_checked_date = models.DateField(blank=True, null=True)

    creation_date = models.DateField(auto_now_add=True, editable=False, blank=True, null=True)

    creation_user = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, related_name='parts_created')

    responsible = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, related_name='parts_responible')

    def format_barcode(self, **kwargs):
        """ Return a JSON string for formatting a barcode for this Ingredient object """

        return helpers.MakeBarcode(
            "part",
            self.id,
            {
                "name": self.full_name,
                "url": reverse('api-part-detail', kwargs={'pk': self.id}),
            },
            **kwargs
        )

    @property
    def category_path(self):
        if self.category:
            return self.category.pathstring
        return ''

    @property
    def available_stock(self):
        """
        Return the total available stock.

        - This subtracts stock which is already allocated to builds
        """

        total = self.total_stock
        total -= self.allocation_count()

        return max(total, 0)

    def requiring_build_orders(self):
        """
        Return list of outstanding build orders which require this part
        """

        # List parts that this part is required for
        parts = self.get_used_in().all()

        part_ids = [part.pk for part in parts]

        # Now, get a list of outstanding build orders which require this part
        builds = BuildModels.Build.objects.filter(
            part__in=part_ids,
            status__in=BuildStatus.ACTIVE_CODES
        )

        return builds

    def required_build_order_quantity(self):
        """
        Return the quantity of this part required for active build orders
        """

        # List active build orders which reference this part
        builds = self.requiring_build_orders()

        quantity = 0

        for build in builds:
    
            bom_item = None

            # List the bom lines required to make the build (including inherited ones!)
            bom_items = build.part.get_bom_items().filter(sub_part=self)

            # Match BOM item to build
            for bom_item in bom_items:

                build_quantity = build.quantity * bom_item.quantity

                quantity += build_quantity
        
        return quantity

    def requiring_sales_orders(self):
        """
        Return a list of sales orders which require this part
        """

        orders = set()

        # Get a list of line items for open orders which match this part
        open_lines = OrderModels.SalesOrderLineItem.objects.filter(
            order__status__in=SalesOrderStatus.OPEN,
            part=self
        )

        for line in open_lines:
            orders.add(line.order)

        return orders

    def required_sales_order_quantity(self):
        """
        Return the quantity of this part required for active sales orders
        """

        # Get a list of line items for open orders which match this part
        open_lines = OrderModels.SalesOrderLineItem.objects.filter(
            order__status__in=SalesOrderStatus.OPEN,
            part=self
        )

        quantity = 0

        for line in open_lines:
            quantity += line.quantity

        return quantity

    def required_order_quantity(self):
        """
        Return total required to fulfil orders
        """

        return self.required_build_order_quantity() + self.required_sales_order_quantity()

    @property
    def quantity_to_order(self):
        """
        Return the quantity needing to be ordered for this part.
        
        Here, an "order" could be one of:
        - Build Order
        - Sales Order

        To work out how many we need to order:

        Stock on hand = self.total_stock
        Required for orders = self.required_order_quantity()
        Currently on order = self.on_order
        Currently building = self.quantity_being_built
        
        """

        # Total requirement
        required = self.required_order_quantity()

        # Subtract stock levels
        required -= max(self.total_stock, self.minimum_stock)

        # Subtract quantity on order
        required -= self.on_order

        # Subtract quantity being built
        required -= self.quantity_being_built

        return max(required, 0)

    @property
    def net_stock(self):
        """ Return the 'net' stock. It takes into account:

        - Stock on hand (total_stock)
        - Stock on order (on_order)
        - Stock allocated (allocation_count)

        This number (unlike 'available_stock') can be negative.
        """

        return self.total_stock - self.allocation_count() + self.on_order

    def isStarredBy(self, user):
        """ Return True if this part has been starred by a particular user """

        try:
            IngredientStar.objects.get(part=self, user=user)
            return True
        except IngredientStar.DoesNotExist:
            return False

    def setStarred(self, user, starred):
        """
        Set the "starred" status of this Ingredient for the given user
        """

        if not user:
            return

        # Do not duplicate efforts
        if self.isStarredBy(user) == starred:
            return

        if starred:
            IngredientStar.objects.create(part=self, user=user)
        else:
            IngredientStar.objects.filter(part=self, user=user).delete()

    def need_to_restock(self):
        """ Return True if this part needs to be restocked
        (either by purchasing or building).

        If the allocated_stock exceeds the total_stock,
        then we need to restock.
        """

        return (self.total_stock + self.on_order - self.allocation_count) < self.minimum_stock

    @property
    def can_build(self):
        """ Return the number of units that can be build with available stock
        """

        # If this part does NOT have a BOM, result is simply the currently available stock
        if not self.has_bom:
            return 0

        total = None

        bom_items = self.get_bom_items().prefetch_related('sub_part__stock_items')

        # Calculate the minimum number of parts that can be built using each sub-part
        for item in bom_items.all():
            stock = item.sub_part.available_stock

            # If (by some chance) we get here but the BOM item quantity is invalid,
            # ignore!
            if item.quantity <= 0:
                continue

            n = int(stock / item.quantity)

            if total is None or n < total:
                total = n

        if total is None:
            total = 0
        
        return max(total, 0)

    @property
    def active_builds(self):
        """ Return a list of outstanding builds.
        Builds marked as 'complete' or 'cancelled' are ignored
        """

        return self.builds.filter(status__in=BuildStatus.ACTIVE_CODES)

    @property
    def inactive_builds(self):
        """ Return a list of inactive builds
        """

        return self.builds.exclude(status__in=BuildStatus.ACTIVE_CODES)

    @property
    def quantity_being_built(self):
        """
        Return the current number of parts currently being built.

        Note: This is the total quantity of Build orders, *not* the number of build outputs.
              In this fashion, it is the "projected" quantity of builds
        """

        builds = self.active_builds

        quantity = 0

        for build in builds:
            # The remaining items in the build
            quantity += build.remaining

        return quantity

    def build_order_allocations(self):
        """
        Return all 'BuildItem' objects which allocate this part to Build objects
        """

        return BuildModels.BuildItem.objects.filter(stock_item__part__id=self.id)

    def build_order_allocation_count(self):
        """
        Return the total amount of this part allocated to build orders
        """

        query = self.build_order_allocations().aggregate(total=Coalesce(Sum('quantity'), 0))

        return query['total']

    def sales_order_allocations(self):
        """
        Return all sales-order-allocation objects which allocate this part to a SalesOrder
        """

        return OrderModels.SalesOrderAllocation.objects.filter(item__part__id=self.id)

    def sales_order_allocation_count(self):
        """
        Return the tutal quantity of this part allocated to sales orders
        """

        query = self.sales_order_allocations().aggregate(total=Coalesce(Sum('quantity'), 0))

        return query['total']

    def allocation_count(self):
        """
        Return the total quantity of stock allocated for this part,
        against both build orders and sales orders.
        """

        return sum([
            self.build_order_allocation_count(),
            self.sales_order_allocation_count(),
        ])

    def stock_entries(self, include_variants=True, in_stock=None):
        """ Return all stock entries for this Ingredient.

        - If this is a template part, include variants underneath this.

        Note: To return all stock-entries for all part variants under this one,
        we need to be creative with the filtering.
        """

        if include_variants:
            query = StockModels.StockItem.objects.filter(part__in=self.get_descendants(include_self=True))
        else:
            query = self.stock_items

        if in_stock is True:
            query = query.filter(StockModels.StockItem.IN_STOCK_FILTER)
        elif in_stock is False:
            query = query.exclude(StockModels.StockItem.IN_STOCK_FILTER)

        return query

    @property
    def total_stock(self):
        """ Return the total stock quantity for this part.
        
        - Ingredient may be stored in multiple locations
        - If this part is a "template" (variants exist) then these are counted too
        """

        entries = self.stock_entries(in_stock=True)

        query = entries.aggregate(t=Coalesce(Sum('quantity'), Decimal(0)))

        return query['t']

    def get_bom_item_filter(self, include_inherited=True):
        """
        Returns a query filter for all BOM items associated with this Ingredient.

        There are some considerations:

        a) BOM items can be defined against *this* part
        b) BOM items can be inherited from a *parent* part

        We will construct a filter to grab *all* the BOM items!

        Note: This does *not* return a queryset, it returns a Q object,
              which can be used by some other query operation!
              Because we want to keep our code DRY!

        """

        bom_filter = Q(part=self)

        if include_inherited:
            # We wish to include parent parts

            parents = self.get_ancestors(include_self=False)

            # There are parents available
            if parents.count() > 0:
                parent_ids = [p.pk for p in parents]

                parent_filter = Q(
                    part__id__in=parent_ids,
                    inherited=True
                )

                # OR the filters together
                bom_filter |= parent_filter

        return bom_filter

    def get_bom_items(self, include_inherited=True):
        """
        Return a queryset containing all BOM items for this part

        By default, will include inherited BOM items
        """

        return BomItem.objects.filter(self.get_bom_item_filter(include_inherited=include_inherited))

    def get_used_in_filter(self, include_inherited=True):
        """
        Return a query filter for all parts that this part is used in.

        There are some considerations:

        a) This part may be directly specified against a BOM for a part
        b) This part may be specifed in a BOM which is then inherited by another part

        Note: This function returns a Q object, not an actual queryset.
              The Q object is used to filter against a list of Ingredient objects
        """

        # This is pretty expensive - we need to traverse multiple variant lists!
        # TODO - In the future, could this be improved somehow?

        # Keep a set of Ingredient ID values
        parts = set()

        # First, grab a list of all BomItem objects which "require" this part
        bom_items = BomItem.objects.filter(sub_part=self)

        for bom_item in bom_items:

            # Add the directly referenced part
            parts.add(bom_item.part)

            # Traverse down the variant tree?
            if include_inherited and bom_item.inherited:

                part_variants = bom_item.part.get_descendants(include_self=False)

                for variant in part_variants:
                    parts.add(variant)

        # Turn into a list of valid IDs (for matching against a Ingredient query)
        part_ids = [part.pk for part in parts]

        return Q(id__in=part_ids)

    def get_used_in(self, include_inherited=True):
        """
        Return a queryset containing all parts this part is used in.

        Includes consideration of inherited BOMs
        """
        return Ingredient.objects.filter(self.get_used_in_filter(include_inherited=include_inherited))

    @property
    def has_bom(self):
        return self.get_bom_items().count() > 0

    @property
    def has_trackable_parts(self):
        """
        Return True if any parts linked in the Bill of Materials are trackable.
        This is important when building the part.
        """

        for bom_item in self.get_bom_items().all():
            if bom_item.sub_part.trackable:
                return True

        return False

    @property
    def bom_count(self):
        """ Return the number of items contained in the BOM for this part """
        return self.get_bom_items().count()

    @property
    def used_in_count(self):
        """ Return the number of part BOMs that this part appears in """
        return self.get_used_in().count()

    def get_bom_hash(self):
        """ Return a checksum hash for the BOM for this part.
        Used to determine if the BOM has changed (and needs to be signed off!)

        The hash is calculated by hashing each line item in the BOM.

        returns a string representation of a hash object which can be compared with a stored value
        """

        hash = hashlib.md5(str(self.id).encode())

        # List *all* BOM items (including inherited ones!)
        bom_items = self.get_bom_items().all().prefetch_related('sub_part')

        for item in bom_items:
            hash.update(str(item.get_item_hash()).encode())

        return str(hash.digest())

    def is_bom_valid(self):
        """ Check if the BOM is 'valid' - if the calculated checksum matches the stored value
        """

        return self.get_bom_hash() == self.bom_checksum

    @transaction.atomic
    def validate_bom(self, user):
        """ Validate the BOM (mark the BOM as validated by the given User.

        - Calculates and stores the hash for the BOM
        - Saves the current date and the checking user
        """

        # Validate each line item, ignoring inherited ones
        bom_items = self.get_bom_items(include_inherited=False)

        for item in bom_items.all():
            item.validate_hash()

        self.bom_checksum = self.get_bom_hash()
        self.bom_checked_by = user
        self.bom_checked_date = datetime.now().date()

        self.save()

    @transaction.atomic
    def clear_bom(self):
        """
        Clear the BOM items for the part (delete all BOM lines).

        Note: Does *NOT* delete inherited BOM items!
        """

        self.bom_items.all().delete()

    def getRequiredIngredients(self, recursive=False, parts=None):
        """
        Return a list of parts required to make this part (i.e. BOM items).

        Args:
            recursive: If True iterate down through sub-assemblies
            parts: Set of parts already found (to prevent recursion issues)
        """

        if parts is None:
            parts = set()

        bom_items = self.get_bom_items().all()

        for bom_item in bom_items:

            sub_part = bom_item.sub_part

            if sub_part not in parts:

                parts.add(sub_part)

                if recursive:
                    sub_part.getRequiredIngredients(recursive=True, parts=parts)

        return parts

    def get_allowed_bom_items(self):
        """
        Return a list of parts which can be added to a BOM for this part.

        - Exclude parts which are not 'component' parts
        - Exclude parts which this part is in the BOM for
        """

        # Start with a list of all parts designated as 'sub components'
        parts = Ingredient.objects.filter(component=True)
        
        # Exclude this part
        parts = parts.exclude(id=self.id)

        # Exclude any parts that this part is used *in* (to prevent recursive BOMs)
        used_in = self.get_used_in().all()

        parts = parts.exclude(id__in=[item.part.id for item in used_in])

        return parts

    @property
    def supplier_count(self):
        """ Return the number of supplier parts available for this part """
        return self.supplier_parts.count()

    @property
    def has_pricing_info(self):
        """ Return true if there is pricing information for this part """
        return self.get_price_range() is not None

    @property
    def has_complete_bom_pricing(self):
        """ Return true if there is pricing information for each item in the BOM. """

        for item in self.get_bom_items().all().select_related('sub_part'):
            if not item.sub_part.has_pricing_info:
                return False

        return True

    def get_price_info(self, quantity=1, buy=True, bom=True):
        """ Return a simplified pricing string for this part
        
        Args:
            quantity: Number of units to calculate price for
            buy: Include supplier pricing (default = True)
            bom: Include BOM pricing (default = True)
        """

        price_range = self.get_price_range(quantity, buy, bom)

        if price_range is None:
            return None

        min_price, max_price = price_range

        if min_price == max_price:
            return min_price

        min_price = normalize(min_price)
        max_price = normalize(max_price)

        return "{a} - {b}".format(a=min_price, b=max_price)

    def get_supplier_price_range(self, quantity=1):
        
        min_price = None
        max_price = None

        for supplier in self.supplier_parts.all():

            price = supplier.get_price(quantity)

            if price is None:
                continue

            if min_price is None or price < min_price:
                min_price = price

            if max_price is None or price > max_price:
                max_price = price

        if min_price is None or max_price is None:
            return None

        min_price = normalize(min_price)
        max_price = normalize(max_price)

        return (min_price, max_price)

    def get_bom_price_range(self, quantity=1):
        """ Return the price range of the BOM for this part.
        Adds the minimum price for all components in the BOM.

        Note: If the BOM contains items without pricing information,
        these items cannot be included in the BOM!
        """

        min_price = None
        max_price = None

        for item in self.get_bom_items().all().select_related('sub_part'):

            if item.sub_part.pk == self.pk:
                print("Warning: Item contains itself in BOM")
                continue

            prices = item.sub_part.get_price_range(quantity * item.quantity)

            if prices is None:
                continue

            low, high = prices

            if min_price is None:
                min_price = 0

            if max_price is None:
                max_price = 0

            min_price += low
            max_price += high

        if min_price is None or max_price is None:
            return None

        min_price = normalize(min_price)
        max_price = normalize(max_price)

        return (min_price, max_price)

    def get_price_range(self, quantity=1, buy=True, bom=True):
        
        """ Return the price range for this part. This price can be either:

        - Supplier price (if purchased from suppliers)
        - BOM price (if built from other parts)

        Returns:
            Minimum of the supplier price or BOM price. If no pricing available, returns None
        """

        buy_price_range = self.get_supplier_price_range(quantity) if buy else None
        bom_price_range = self.get_bom_price_range(quantity) if bom else None

        if buy_price_range is None:
            return bom_price_range

        elif bom_price_range is None:
            return buy_price_range

        else:
            return (
                min(buy_price_range[0], bom_price_range[0]),
                max(buy_price_range[1], bom_price_range[1])
            )

    @transaction.atomic
    def copy_bom_from(self, other, clear=True, **kwargs):
        """
        Copy the BOM from another part.

        args:
            other - The part to copy the BOM from
            clear - Remove existing BOM items first (default=True)
        """

        if clear:
            # Remove existing BOM items
            # Note: Inherited BOM items are *not* deleted!
            self.bom_items.all().delete()

        # Copy existing BOM items from another part
        # Note: Inherited BOM Items will *not* be duplicated!!
        for bom_item in other.get_bom_items(include_inherited=False).all():
            # If this part already has a BomItem pointing to the same sub-part,
            # delete that BomItem from this part first!

            try:
                existing = BomItem.objects.get(part=self, sub_part=bom_item.sub_part)
                existing.delete()
            except (BomItem.DoesNotExist):
                pass

            bom_item.part = self
            bom_item.pk = None

            bom_item.save()

    @transaction.atomic
    def copy_parameters_from(self, other, **kwargs):
        
        clear = kwargs.get('clear', True)

        if clear:
            self.get_parameters().delete()

        for parameter in other.get_parameters():

            # If this part already has a parameter pointing to the same template,
            # delete that parameter from this part first!

            try:
                existing = IngredientParameter.objects.get(part=self, template=parameter.template)
                existing.delete()
            except (IngredientParameter.DoesNotExist):
                pass

            parameter.part = self
            parameter.pk = None

            parameter.save()

    @transaction.atomic
    def deep_copy(self, other, **kwargs):
        """ Duplicates non-field data from another part.
        Does not alter the normal fields of this part,
        but can be used to copy other data linked by ForeignKey refernce.

        Keyword Args:
            image: If True, copies Ingredient image (default = True)
            bom: If True, copies BOM data (default = False)
            parameters: If True, copies Parameters data (default = True)
        """

        # Copy the part image
        if kwargs.get('image', True):
            if other.image:
                # Reference the other image from this Ingredient
                self.image = other.image

        # Copy the BOM data
        if kwargs.get('bom', False):
            self.copy_bom_from(other)

        # Copy the parameters data
        if kwargs.get('parameters', True):
            self.copy_parameters_from(other)
        
        # Copy the fields that aren't available in the duplicate form
        self.salable = other.salable
        self.assembly = other.assembly
        self.component = other.component
        self.purchaseable = other.purchaseable
        self.trackable = other.trackable
        self.virtual = other.virtual

        self.save()

    def getTestTemplates(self, required=None, include_parent=True):
        """
        Return a list of all test templates associated with this Ingredient.
        These are used for validation of a StockItem.

        args:
            required: Set to True or False to filter by "required" status
            include_parent: Set to True to traverse upwards
        """

        if include_parent:
            tests = IngredientTestTemplate.objects.filter(part__in=self.get_ancestors(include_self=True))
        else:
            tests = self.test_templates

        if required is not None:
            tests = tests.filter(required=required)

        return tests
    
    def getRequiredTests(self):
        # Return the tests which are required by this part
        return self.getTestTemplates(required=True)

    def requiredTestCount(self):
        return self.getRequiredTests().count()

    @property
    def attachment_count(self):
        """ Count the number of attachments for this part.
        If the part is a variant of a template part,
        include the number of attachments for the template part.

        """

        return self.part_attachments.count()

    @property
    def part_attachments(self):
        """
        Return *all* attachments for this part,
        potentially including attachments for template parts
        above this one.
        """

        ancestors = self.get_ancestors(include_self=True)

        attachments = IngredientAttachment.objects.filter(part__in=ancestors)

        return attachments

    def sales_orders(self):
        """ Return a list of sales orders which reference this part """

        orders = []

        for line in self.sales_order_line_items.all().prefetch_related('order'):
            if line.order not in orders:
                orders.append(line.order)

        return orders

    def purchase_orders(self):
        """ Return a list of purchase orders which reference this part """

        orders = []

        for part in self.supplier_parts.all().prefetch_related('purchase_order_line_items'):
            for order in part.purchase_orders():
                if order not in orders:
                    orders.append(order)

        return orders

    def open_purchase_orders(self):
        """ Return a list of open purchase orders against this part """

        return [order for order in self.purchase_orders() if order.status in PurchaseOrderStatus.OPEN]

    def closed_purchase_orders(self):
        """ Return a list of closed purchase orders against this part """

        return [order for order in self.purchase_orders() if order.status not in PurchaseOrderStatus.OPEN]

    @property
    def on_order(self):
        """ Return the total number of items on order for this part. """

        orders = self.supplier_parts.filter(purchase_order_line_items__order__status__in=PurchaseOrderStatus.OPEN).aggregate(
            quantity=Sum('purchase_order_line_items__quantity'),
            received=Sum('purchase_order_line_items__received')
        )

        quantity = orders['quantity']
        received = orders['received']

        if quantity is None:
            quantity = 0

        if received is None:
            received = 0

        return quantity - received

    def get_parameters(self):
        """ Return all parameters for this part, ordered by name """

        return self.parameters.order_by('template__name')

    @property
    def has_variants(self):
        """ Check if this Ingredient object has variants underneath it. """

        return self.get_all_variants().count() > 0

    def get_all_variants(self):
        """ Return all Ingredient object which exist as a variant under this part. """

        return self.get_descendants(include_self=False)

    def get_related_parts(self):
        """ Return list of tuples for all related parts:
            - first value is IngredientRelated object
            - second value is matching Ingredient object
        """

        related_parts = []

        related_parts_1 = self.related_parts_1.filter(part_1__id=self.pk)

        related_parts_2 = self.related_parts_2.filter(part_2__id=self.pk)

        for related_part in related_parts_1:
            # Add to related parts list
            related_parts.append((related_part, related_part.part_2))

        for related_part in related_parts_2:
            # Add to related parts list
            related_parts.append((related_part, related_part.part_1))

        return related_parts

    @property
    def related_count(self):
        return len(self.get_related_parts())

class IngredientParameter(models.Model):
    """
    A IngredientParameter is a specific instance of a IngredientParameterTemplate. It assigns a particular parameter <key:value> pair to a part.

    Attributes:
        part: Reference to a single Ingredient object
        template: Reference to a single IngredientParameterTemplate object
        data: The data (value) of the Parameter [string]
    """

    def __str__(self):
        # String representation of a IngredientParameter (used in the admin interface)
        return "{part} : {param} = {data}{units}".format(
            part=str(self.part.full_name),
            param=str(self.template.name),
            data=str(self.data),
            units=str(self.template.units)
        )

    class Meta:
        # Prevent multiple instances of a parameter for a single part
        unique_together = ('part', 'template')

    part = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name='parameters', help_text=_('Parent Ingredient'))

    template = models.ForeignKey(IngredientParameterTemplate, on_delete=models.CASCADE, related_name='instances', help_text=_('Parameter Template'))

    data = models.CharField(max_length=500, help_text=_('Parameter Value'))

    @classmethod
    def create(cls, part, template, data, save=False):
        part_parameter = cls(part=part, template=template, data=data)
        if save:
            part_parameter.save()
        return part_parameter

class IngredientStar(models.Model):
    """ A IngredientStar object creates a relationship between a User and a Ingredient.

    It is used to designate a Ingredient as 'starred' (or favourited) for a given User,
    so that the user can track a list of their favourite parts.

    Attributes:
        part: Link to a Ingredient object
        user: Link to a User object
    """

    part = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name='starred_users')

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='starred_parts')

    class Meta:
        unique_together = ['part', 'user']


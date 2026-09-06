"""Root URLconf -- a safety net only.

Every served request is routed by ``osds.middleware.TenantResolutionMiddleware``
to ``osds.urls_console`` or ``osds.urls_tenant`` (or, during first-run, the
setup wizard). Unknown hosts get a 404 from the middleware before resolution.
"""

urlpatterns: list = []

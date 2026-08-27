from django.conf import settings
from django.http import HttpResponsePermanentRedirect


class ConditionalHttpsMiddleware:
    """
    SESSION_COOKIE_SECURE/CSRF_COOKIE_SECURE=True ломают логин по прямому
    LAN-доступу (http://10.0.0.2:9091 и т.п.), который всё ещё используется
    в обход публичной прокси-цепочки. Поэтому HTTPS-редирект и Secure-флаг
    на куках включаем только для settings.PUBLIC_HTTPS_HOST — для остальных
    хостов из ALLOWED_HOSTS (localhost, 10.0.0.2, ...) поведение как без
    харднинга.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().split(':', 1)[0]
        is_public = host == settings.PUBLIC_HTTPS_HOST

        if is_public and not request.is_secure():
            return HttpResponsePermanentRedirect(
                'https://' + request.get_host() + request.get_full_path()
            )

        response = self.get_response(request)

        if not is_public:
            for cookie in response.cookies.values():
                cookie['secure'] = False

        return response

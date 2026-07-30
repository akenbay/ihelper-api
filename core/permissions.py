"""Ролевые permission-классы.

Права режутся в двух местах, и оба обязательны:
  1) permission-класс — можно ли вообще дёргать этот эндпоинт этой ролью;
  2) get_queryset() во вьюсете — какие ОБЪЕКТЫ роль видит (тьютор — только своих
     учеников, родитель — только своих детей).

Одного permission-класса мало: без сужения queryset тьютор смог бы прочитать
чужой журнал по прямому id.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Role


class IsAdmin(BasePermission):
    message = "Действие доступно только администратору."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_admin)


class IsCoordinator(BasePermission):
    message = "Действие доступно только координатору."

    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and request.user.is_coordinator
        )


class IsTutor(BasePermission):
    message = "Действие доступно только тьютору."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_tutor)


class IsParent(BasePermission):
    message = "Действие доступно только родителю."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_parent)


class IsAdminOrCoordinator(BasePermission):
    message = "Действие доступно только администратору или координатору."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated and (user.is_admin or user.is_coordinator)
        )


class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS


class IsAdminOrCoordinatorOrReadOnly(BasePermission):
    """Писать могут админ и координатор; остальные аутентифицированные — читать.

    Объекты всё равно сужаются в get_queryset(): «читать» ≠ «читать всё».
    """

    message = "Изменять эти данные может только координатор или администратор."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return user.is_admin or user.is_coordinator


class IsAdminOrReadOnly(BasePermission):
    """Писать может только админ; остальные аутентифицированные — читать.

    Для общей базы материалов: её курирует администратор на всю организацию,
    координаторы только читают.
    """

    message = "Изменять эти данные может только администратор."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return user.is_admin


class RolePermission(BasePermission):
    """Разрешает метод, если роль есть в карте прав вьюсета.

    Вьюсет объявляет:
        allowed_roles = {
            "read":  [Role.ADMIN, Role.COORDINATOR, Role.TUTOR, Role.PARENT],
            "write": [Role.TUTOR],
        }
    """

    message = "У вашей роли нет прав на это действие."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False

        allowed = getattr(view, "allowed_roles", {})
        bucket = "read" if request.method in SAFE_METHODS else "write"
        roles = allowed.get(bucket, [])

        role = Role.ADMIN if user.is_admin else user.role
        return role in roles

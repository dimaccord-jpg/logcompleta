import os
from flask import Blueprint, render_template, redirect, url_for, request
from flask_login import login_required, current_user, logout_user

from app.models import User
from app.auth_services import encerrar_contrato


base_dir = os.path.dirname(os.path.abspath(__file__))

user_bp = Blueprint(
    "user",
    __name__,
    template_folder=os.path.join(base_dir, "templates"),
)


@user_bp.route("/perfil")
@login_required
def perfil():
    """
    Área do Usuário autenticado.

    A lógica de segurança (login_required) e de autorização adicional
    permanece delegada ao Flask-Login e ao modelo User, evitando hardcodes.
    """
    assert isinstance(current_user._get_current_object(), User)  # type: ignore[attr-defined]
    return render_template("user_area.html")


@user_bp.route("/perfil/encerrar-contrato", methods=["POST"])
@login_required
def encerrar_contrato_route():
    """
    Encerra o contrato do usuário: anonimiza dados, invalida sessão e redireciona.
    Deve ser chamado após confirmação no modal (POST).
    """
    user = current_user._get_current_object()
    if not isinstance(user, User):
        return redirect(url_for("user.perfil"))
    encerrar_contrato(user)
    logout_user()
    return redirect(url_for("index"))


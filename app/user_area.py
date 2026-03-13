import os
from flask import Blueprint, render_template
from flask_login import login_required, current_user

from app.models import User


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
    # A view em si mantém responsabilidade apenas de orquestrar o template.
    # Dados específicos do usuário são expostos via `current_user` no Jinja.
    assert isinstance(current_user._get_current_object(), User)  # type: ignore[attr-defined]
    return render_template("user_area.html")


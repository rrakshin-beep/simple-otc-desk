
from pathlib import Path

path = Path("app/templates/report.html")
text = path.read_text(encoding="utf-8")

old_header = (
    "<th>Тип вводной</th><th>Данные заявки</th>"
)

new_header = (
    "<th>Сумма FIAT</th><th>Сумма CRYPTO</th>"
)

if old_header not in text:
    raise SystemExit(
        "Не найдены столбцы 'Тип вводной' и 'Данные заявки' в app/templates/report.html"
    )

text = text.replace(old_header, new_header, 1)

old_cells = (
    "<td>{{r.amount_type.value}}</td>"
    "<td>{% if r.amount_type.value == 'FIAT' %}"
    "{{r.fiat_amount}} {{r.quote_asset}}"
    "{% else %}"
    "{{r.amount}} {{r.base_asset}}"
    "{% endif %}</td>"
)

new_cells = (
    "<td>{{r.fiat_amount}} {{r.quote_asset}}</td>"
    "<td>{{r.amount}} {{r.base_asset}}</td>"
)

if old_cells not in text:
    raise SystemExit(
        "Не найден ожидаемый блок данных заявки в app/templates/report.html"
    )

text = text.replace(old_cells, new_cells, 1)
path.write_text(text, encoding="utf-8")

print("Обновлен: app/templates/report.html")
print("Удалены столбцы: Тип вводной, Данные заявки")
print("Добавлены столбцы: Сумма FIAT, Сумма CRYPTO")

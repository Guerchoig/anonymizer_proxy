import docx
import re

doc = docx.Document(r'c:\Test\anonymizer_proxy\docs\KP_IRIS.result.docx')
table = doc.tables[3]

def parse_cost(text):
    text = text.strip().replace(' ', '').replace(',', '.')
    try:
        return float(text)
    except:
        return None

# Собираем данные строк
rows_data = []
for i, row in enumerate(table.rows):
    cells = [cell.text.strip() for cell in row.cells]
    task = cells[0]
    cost = parse_cost(cells[4]) if len(cells) > 4 else None
    rows_data.append((i, task, cost))

# Определяем иерархию
groups = {}
current_main_group = None
current_sub_group = None

for i, task, cost in rows_data:
    if i == 0:
        continue
    main_match = re.match(r'^(\d+)\.\s', task)
    sub_match = re.match(r'^(\d+\.\d+)\s', task)
    root_match = re.match(r'^Внедрение', task)

    if root_match:
        current_main_group = i
        current_sub_group = None
        groups[i] = {'name': task, 'cost': cost, 'children': [], 'level': 'root'}
    elif sub_match:
        current_sub_group = i
        groups[i] = {'name': task, 'cost': cost, 'children': [], 'level': 'sub', 'parent': current_main_group}
        groups[current_main_group]['children'].append(i)
    elif main_match:
        current_main_group = i
        current_sub_group = None
        groups[i] = {'name': task, 'cost': cost, 'children': [], 'level': 'main'}
        # Добавляем основной этап как дочерний элемент корневой группы
        if 1 in groups and groups[1]['level'] == 'root':
            groups[1]['children'].append(i)
    else:
        if current_sub_group is not None:
            groups[current_sub_group]['children'].append(i)
        elif current_main_group is not None:
            groups[current_main_group]['children'].append(i)

def fmt(v):
    if v is None:
        return 'N/A'
    return f'{v:,.2f}'.replace(',', ' ')

print('=' * 80)
print('ПРОВЕРКА СУММИРОВАНИЯ ЗАТРАТ ПО ГРУППИРОВКАМ')
print('=' * 80)

all_ok = True
results = []

for idx in sorted(groups.keys()):
    g = groups[idx]
    if g['level'] == 'root':
        continue

    children_costs = []
    for child_idx in g['children']:
        if child_idx in groups:
            children_costs.append(groups[child_idx]['cost'])
        else:
            _, _, cost = rows_data[child_idx]
            children_costs.append(cost)

    children_sum = sum(c for c in children_costs if c is not None)
    group_cost = g['cost']

    diff = abs(children_sum - group_cost) if group_cost is not None and children_sum is not None else 0
    ok = diff < 0.01
    if not ok:
        all_ok = False

    status = 'OK' if ok else f'РАСХОЖДЕНИЕ (разница {diff:,.2f})'.replace(',', ' ')
    results.append({
        'level': g['level'],
        'name': g['name'],
        'declared': group_cost,
        'computed': children_sum,
        'diff': diff,
        'ok': ok,
        'status': status,
    })

# Корневая группировка
root_idx = 1
root = groups[root_idx]
main_groups_sum = sum(groups[g_idx]['cost'] for g_idx in root['children'] if g_idx in groups)
root_diff = abs(main_groups_sum - root['cost']) if root['cost'] is not None else 0
root_ok = root_diff < 0.01
if not root_ok:
    all_ok = False

# Вывод
for r in results:
    print()
    print(f"Группа [{r['level']}]: {r['name']}")
    print(f"  Заявленная сумма:  {fmt(r['declared'])} руб.")
    print(f"  Сумма дочерних:    {fmt(r['computed'])} руб.")
    print(f"  Статус: {r['status']}")

print()
print('=' * 80)
print(f"КОРНЕВАЯ ГРУППИРОВКА: {root['name']}")
print(f"  Заявленная сумма:  {fmt(root['cost'])} руб.")
print(f"  Сумма этапов:      {fmt(main_groups_sum)} руб.")
root_status = 'OK' if root_ok else f'РАСХОЖДЕНИЕ (разница {root_diff:,.2f})'.replace(',', ' ')
print(f"  Статус: {root_status}")

print()
print('=' * 80)
print(f'ИТОГ: {"ВСЕ СОВПАДАЕТ ✓" if all_ok else "ЕСТЬ РАСХОЖДЕНИЯ ✗"}')
print('=' * 80)

from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from copy import deepcopy

def add_column_to_table(table):
    """Добавляет столбец справа в таблицу"""
    # Проходим по всем строкам таблицы
    for row in table.rows:
        # Создаем новую ячейку
        new_cell = OxmlElement('w:tc')
        
        # Копируем свойства ячейки из последней ячейки строки (если есть)
        if row.cells:
            last_cell = row.cells[-1]
            tc_pr = last_cell._element.find(qn('w:tcPr'))
            if tc_pr is not None:
                new_tc_pr = deepcopy(tc_pr)
                new_cell.insert(0, new_tc_pr)
        
        # Добавляем пустой параграф в ячейку
        p = OxmlElement('w:p')
        new_cell.append(p)
        
        # Добавляем ячейку в конец строки
        row._element.append(new_cell)
    
    # Устанавливаем заголовок "Комментарий" в первой ячейке нового столбца
    if table.rows:
        first_row = table.rows[0]
        if first_row.cells:
            # Получаем последнюю ячейку (новый столбец)
            last_cell = first_row.cells[-1]
            # Очищаем ячейку и добавляем текст
            last_cell.text = "Комментарий"

def process_document(input_path, output_path):
    """Обрабатывает документ, добавляя столбец в каждую таблицу"""
    doc = Document(input_path)
    
    # Обрабатываем все таблицы в документе
    for table in doc.tables:
        add_column_to_table(table)
    
    # Сохраняем документ
    doc.save(output_path)
    print(f"Обработан: {input_path} -> {output_path}")

# Обрабатываем оба файла
process_document(
    r"c:\Test\anonymizer_proxy\docs\KP_IRIS.anonymized.docx",
    r"c:\Test\anonymizer_proxy\docs\KP_IRIS.anonymized.docx"
)

process_document(
    r"c:\Test\anonymizer_proxy\docs\КП ДО 01.anonymized.docx",
    r"c:\Test\anonymizer_proxy\docs\КП ДО 01.anonymized.docx"
)

print("Готово! Столбец 'Комментарий' добавлен во все таблицы.")

from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from lxml import etree

def add_column_to_all_tables(doc):
    """Добавляет столбец 'Комментарий' во все таблицы"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    
    for table in doc.tables:
        # Добавляем ячейку в каждую строку через XML
        for i, row in enumerate(table.rows):
            # Создаем новую ячейку
            tc = OxmlElement('w:tc')
            tcPr = OxmlElement('w:tcPr')
            tc.append(tcPr)
            
            # Добавляем параграф с текстом
            p = OxmlElement('w:p')
            r = OxmlElement('w:r')
            
            # Для первой строки добавляем заголовок
            if i == 0:
                rPr = OxmlElement('w:rPr')
                b = OxmlElement('w:b')
                rPr.append(b)
                r.append(rPr)
                
                t = OxmlElement('w:t')
                t.text = "Комментарий"
                r.append(t)
                
                # Центрирование
                pPr = OxmlElement('w:pPr')
                jc = OxmlElement('w:jc')
                jc.set(qn('w:val'), 'center')
                pPr.append(jc)
                p.insert(0, pPr)
            
            p.append(r)
            tc.append(p)
            row._tr.append(tc)

def find_toc_end(doc):
    """Ищет конец содержания (TOC)"""
    # Ищем параграф с "Содержание" или "Оглавление"
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip().lower()
        if text in ['содержание', 'оглавление', 'table of contents']:
            # Нашли начало TOC, ищем конец
            # TOC обычно заканчивается перед следующим Heading
            for j in range(i + 1, len(doc.paragraphs)):
                next_para = doc.paragraphs[j]
                if next_para.style.name.startswith('Heading'):
                    return j
            # Если не нашли Heading, возвращаем позицию после TOC
            return i + 10  # предполагаем, что TOC занимает несколько строк
    return 0

def add_about_section_after_toc(doc, text):
    """Добавляет раздел 'О документе' после содержания"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    
    toc_end = find_toc_end(doc)
    
    # Вставляем после TOC
    if toc_end < len(doc.paragraphs):
        target_para = doc.paragraphs[toc_end]
        
        # Создаем заголовок "О документе"
        heading = target_para.insert_paragraph_before("О документе")
        heading.style = doc.styles['Heading 1']
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        # Создаем параграф с текстом через XML
        # Вставляем его после заголовка
        content_para = heading.insert_paragraph_before("")
        content_para.text = text
        content_para.alignment = WD_ALIGN_PARAGRAPH.LEFT
        
        # Пустой параграф после
        content_para.insert_paragraph_before("")

# Обработка KP_IRIS
print("Обработка KP_IRIS.anonymized.docx...")
doc1 = Document(r'c:\Test\anonymizer_proxy\docs\KP_IRIS.anonymized.docx')
add_column_to_all_tables(doc1)

about_text1 = """Настоящий документ представляет собой коммерческое предложение на оказание услуг по внедрению системы электронного документооборота.

Основные параметры проекта:
• Стоимость проекта включает все работы и лицензии
• Срок реализации охватывает все этапы от обследования до промышленной эксплуатации
• Система рассчитана на множество одновременных пользователей
• Проект включает центральное руководство и территориальные подразделения

Этапы проекта:
1. Обследование организации Заказчика
2. Нагрузочное тестирование
3. Проектирование и разработка решений
4. Настройка системы
5. Внедрение и опытная эксплуатация
6. Промышленная эксплуатация

Цели: автоматизация обработки документов, повышение эффективности, обеспечение контроля документооборота.

По результатам этапов предоставляется документация: отчеты, протоколы, руководства."""

add_about_section_after_toc(doc1, about_text1)
doc1.save(r'c:\Test\anonymizer_proxy\docs\KP_IRIS.anonymized.docx')
print("✓ KP_IRIS.anonymized.docx обработан")

# Обработка КП ДО 01
print("\nОбработка КП ДО 01.anonymized.docx...")
doc2 = Document(r'c:\Test\anonymizer_proxy\docs\КП ДО 01.anonymized.docx')
add_column_to_all_tables(doc2)

about_text2 = """Настоящий документ представляет собой технико-коммерческое предложение на реализацию базовых процессов делопроизводства на базе специализированного программного решения.

Основные направления:
• Внедрение системы электронного документооборота
• Автоматизация базовых процессов делопроизводства
• Интеграция с ИТ-инфраструктурой Заказчика
• Обучение пользователей и администраторов

Этапы проекта:
1. Предпроектное обследование
2. Проектирование архитектуры
3. Настройка и конфигурирование
4. Миграция данных
5. Приемочные испытания
6. Ввод в промышленную эксплуатацию
7. Техническая поддержка

Ожидаемые результаты:
• Централизация управления документами
• Сокращение времени обработки
• Повышение контроля исполнения
• Обеспечение информационной безопасности
• Единое пространство для совместной работы

Документ содержит описание функциональных возможностей, этапы внедрения, стоимость работ и условия поддержки."""

add_about_section_after_toc(doc2, about_text2)
doc2.save(r'c:\Test\anonymizer_proxy\docs\КП ДО 01.anonymized.docx')
print("✓ КП ДО 01.anonymized.docx обработан")

print("\n✓ Готово!")

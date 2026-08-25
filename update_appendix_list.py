from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH


def update_appendix_list_kp_iris(doc):
    """Обновляет список приложений в KP_IRIS - добавляет Приложение 4"""
    
    # Ищем строку "Приложения:" и следующий список
    for i, p in enumerate(doc.paragraphs):
        text = p.text.strip()
        if text == "Приложения:":
            print(f"  Найден список приложений на строке {i}")
            # Находим последнее приложение в списке (Приложение 3)
            for j in range(i+1, min(i+10, len(doc.paragraphs))):
                next_text = doc.paragraphs[j].text.strip()
                if "Приложение 3" in next_text:
                    print(f"  Найдено Приложение 3 на строке {j}: {next_text}")
                    # Добавляем Приложение 4 после него
                    new_p = doc.add_paragraph()
                    new_p.text = "Приложение 4. Шаблон структуры коммерческого предложения."
                    # Вставляем после текущего параграфа
                    doc.paragraphs[j]._element.addnext(new_p._element)
                    print(f"  Добавлено Приложение 4")
                    break
            break


def update_appendix_list_kp_do_01(doc):
    """Обновляет список приложений в КП ДО 01 - добавляет список приложений"""
    
    # Ищем место перед подписью или контактами
    for i, p in enumerate(doc.paragraphs):
        text = p.text.strip()
        if "Контакты" in text or "Приложения" in text:
            print(f"  Найдено место для списка приложений на строке {i}: {text}")
            # Добавляем список приложений перед этим местом
            new_p1 = doc.add_paragraph()
            new_p1.text = "Приложения:"
            
            new_p2 = doc.add_paragraph()
            new_p2.text = "Приложение 1. Шаблон структуры коммерческого предложения."
            
            # Вставляем перед текущим параграфом
            doc.paragraphs[i]._element.addprevious(new_p1._element)
            doc.paragraphs[i]._element.addprevious(new_p2._element)
            print(f"  Добавлен список приложений")
            break


def process_document(input_path, output_path, update_func):
    """Обрабатывает документ: обновляет список приложений"""
    doc = Document(input_path)
    
    print(f"Обработка: {input_path}")
    
    update_func(doc)
    
    doc.save(output_path)
    print(f"Сохранено: {output_path}\n")


# Обновляем оба файла
files = [
    ('c:/Test/anonymizer_proxy/docs/KP_IRIS.result.docx', 
     'c:/Test/anonymizer_proxy/docs/KP_IRIS.result.docx', 
     update_appendix_list_kp_iris),
    ('c:/Test/anonymizer_proxy/docs/КП ДО 01.result.docx', 
     'c:/Test/anonymizer_proxy/docs/КП ДО 01.result.docx', 
     update_appendix_list_kp_do_01)
]

for input_path, output_path, update_func in files:
    process_document(input_path, output_path, update_func)

print("Готово!")

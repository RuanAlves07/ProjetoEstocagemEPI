import re

p = r"C:\Github\ProjetoEstocagemEPI\app\cadastros.py"
with open(p, "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace(
    'return render_template("epi_form.html", epi=dados, novo=True, qtds_por_tam=qtds_por_tam)',
    'return render_template("epi_form.html", epi=dados, novo=True, qtds_por_tam=qtds_por_tam, categorias_padrao=CATEGORIAS_PADRAO)'
)
text = text.replace(
    'return render_template("epi_form.html", epi=epi, novo=False, epi_id=epi_id)',
    'return render_template("epi_form.html", epi=epi, novo=False, epi_id=epi_id, categorias_padrao=CATEGORIAS_PADRAO)'
)

with open(p, "w", encoding="utf-8") as f:
    f.write(text)
print("Updated successfully")

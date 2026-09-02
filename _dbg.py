from stores import TIENDAS
print('tiendas registradas:', len(TIENDAS))
for t in TIENDAS: print(f'  {t.name:12} {t.__class__.__name__}')

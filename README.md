# Importador de Clientes Legacy a Odoo 18

Script para importar clientes desde Excel de sistemas legacy a Odoo 18 Enterprise, con soporte para identificación CUIT/DNI, provincias, y procesamiento paralelo.

## Características

- ✅ **Importación desde Excel**: Lee archivos .xlsx con estructura de clientes legacy
- ✅ **CUIT sin guiones**: Procesa y almacena CUIT limpio (solo números)
- ✅ **Tipo de Identificación**: Asigna automáticamente CUIT (ID=4) o DNI (ID=5)
- ✅ **Mapeo de Provincias**: Asocia ciudades con provincias (Misiones, CABA, Buenos Aires)
- ✅ **Tipos de IVA**: Mapea responsabilidades fiscales (RI, CF, M, EX, etc.)
- ✅ **Procesamiento Paralelo**: Usa ThreadPoolExecutor para importación rápida
- ✅ **Idempotente**: Detecta clientes existentes y los omite o actualiza
- ✅ **Dry-run**: Modo de prueba sin modificar la base de datos
- ✅ **Logging detallado**: Registra cada operación con timestamp

## Requisitos

- Python 3.8+
- Odoo 18 Enterprise con acceso XML-RPC
- Librerías: openpyxl

```bash
pip install openpyxl
```

## Estructura del Excel

El archivo Excel debe contener las siguientes columnas:

| Columna | Descripción | Ejemplo |
|---------|-------------|---------|
| Codigo | Código único del cliente | 20 |
| Nombre | Razón social o nombre completo | PORTAL DEL IGUAZU S.A. |
| Cuit | CUIT o DNI (con o sin guiones) | 30-12345678-9 |
| Tipo IVA | Responsabilidad fiscal | RI, CF, M, EX |
| Domicilio | Dirección completa | Av. Brasil 123 |
| Localidad | Ciudad | Puerto Iguazú |
| Telefono | Teléfono de contacto | 3757-123456 |
| Correo | Email | contacto@empresa.com |

## Uso

### Modo Dry-Run (sin modificar la base de datos)

```bash
python import_legacy_customers.py \
  --excel /ruta/al/archivo/clientes.xlsx \
  --dry-run
```

### Importar Nuevos Clientes

```bash
python import_legacy_customers.py \
  --excel /ruta/al/archivo/clientes.xlsx \
  --execute
```

### Actualizar Clientes Existentes

```bash
python import_legacy_customers.py \
  --excel /ruta/al/archivo/clientes.xlsx \
  --execute \
  --update-existing
```

### Controlar Número de Hilos

Por defecto usa 5 hilos paralelos. Puedes ajustar esto:

```bash
python import_legacy_customers.py \
  --excel /ruta/al/archivo/clientes.xlsx \
  --execute \
  --threads 10
```

## Configuración de Odoo

Edita las credenciales en el script:

```python
# Configuración de conexión a Odoo
ODOO_URL = "https://mundolimpio.aftermoves.com"
ODOO_DB = "Testing"
ODOO_USERNAME = "admin"
ODOO_PASSWORD = "tu_password"
```

## Campos de Odoo Mapeados

### Campos Principales
- `ref`: Código de cliente
- `name`: Nombre/Razón social
- `vat`: CUIT/DNI sin guiones
- `l10n_latam_identification_type_id`: Tipo (CUIT=4, DNI=5)
- `l10n_ar_afip_responsibility_type_id`: Tipo IVA (RI, CF, M, etc.)

### Campos de Contacto
- `street`: Domicilio
- `city`: Localidad
- `state_id`: Provincia (mapeada automáticamente)
- `country_id`: Argentina (ID=10)
- `phone`: Teléfono
- `email`: Correo electrónico

### Flags
- `customer_rank`: 1 (marcado como cliente)
- `company_type`: 'company' o 'person' (según CUIT/DNI)

## Mapeo de Provincias

El script mapea automáticamente ciudades a provincias:

### Misiones (ID=566)
Puerto Iguazú, Posadas, Oberá, Eldorado, Jardín América, etc.

### CABA (ID=553)
Buenos Aires, Capital Federal, CABA

### Buenos Aires (ID=554)
La Plata, Mar del Plata, Bahía Blanca, Quilmes, etc.

## Tipos de Identificación

- **CUIT** (ID=4): 11 dígitos - Empresas y monotributistas
- **DNI** (ID=5): 7-8 dígitos - Personas físicas

## Responsabilidades Fiscales (IVA)

| Código | Descripción |
|--------|-------------|
| RI | Responsable Inscripto |
| CF | Consumidor Final |
| M | Monotributista |
| EX | Exento |
| NC | No Categorizado |
| RNI | Responsable No Inscripto |

## Resultados

El script muestra estadísticas al finalizar:

```
✅ Clientes creados:      1250
🔄 Clientes actualizados: 0
⏭️  Clientes omitidos:     110
❌ Errores:               2
```

### Errores Comunes

1. **VAT duplicado**: El CUIT ya existe en otro partner
   - Solución: Verificar en Odoo si ya existe

2. **CUIT inválido**: No empieza con prefijo válido (20, 23, 24, 27, 30, 33, 34, 50, 51, 55)
   - Solución: Corregir en el Excel

3. **Tipo IVA no encontrado**: Código no existe en Odoo
   - Solución: Usar CF, RI, M, EX, NC o RNI

## Logs

Los logs se guardan con timestamp en la consola:

```
2026-01-31 11:17:01 [INFO] Creado: 1071 - EDUARDO VERON RODRIGUEZ (ID: 102)
2026-01-31 11:17:02 [INFO] SKIP (existe): 1035 - GASTON LUCIANO GARIN (ID: 63)
2026-01-31 11:18:38 [ERROR] Error creando ZONA FRANCA: <Fault 2: 'The VAT 30707036938 already exists'>
```

## Flujo de Trabajo Recomendado

1. **Preparar Excel**: Verificar que tenga todas las columnas requeridas
2. **Dry-run**: Ejecutar en modo prueba para verificar datos
3. **Revisar estadísticas**: Verificar cuántos clientes se importarán
4. **Ejecutar importación**: Correr con `--execute`
5. **Verificar en Odoo**: Revisar algunos clientes en la interfaz web
6. **Re-ejecutar si falla**: El script es idempotente, puede ejecutarse múltiples veces

## Performance

- **Sin hilos**: ~2 clientes/segundo
- **Con 5 hilos** (default): ~8-10 clientes/segundo
- **Con 10 hilos**: ~12-15 clientes/segundo

⚠️ **Nota**: No usar más de 10 hilos para evitar sobrecarga en el servidor Odoo.

## Solución de Problemas

### Timeout de conexión

```bash
# Reducir número de hilos
python import_legacy_customers.py --excel clientes.xlsx --execute --threads 3
```

### Error de conexión SSL

```bash
# Verificar URL y certificados
curl -I https://mundolimpio.aftermoves.com
```

### Cliente no se crea

1. Verificar que el CUIT sea válido
2. Revisar que no exista ya con ese CUIT
3. Verificar permisos del usuario en Odoo

## Migración de Saldos

Después de importar clientes, ejecutar el script de migración de saldos:

```bash
python migrate_legacy_balances.py \
  --excel /ruta/al/saldos.xlsx \
  --execute
```

El script de saldos buscará los clientes importados por su código (campo `ref`).

## Autor

Script desarrollado para migración a Odoo 18 Enterprise - Mundo Limpio Iguazú

## Licencia

MIT

# Migración de Saldos Legacy a Odoo

## 📋 Descripción

Script para migrar saldos de cuentas corrientes de clientes desde un sistema legacy (exportado a Excel) hacia Odoo 18, creando **asientos contables de apertura** (NO facturas).

## 🎯 Objetivo

- Migrar saldos pendientes de clientes
- Conservar fechas de vencimiento para conciliación futura
- Mantener trazabilidad del origen (sucursal, número de factura legacy)
- NO crear facturas fiscales (ya fueron emitidas en el sistema anterior)

## 📊 Modelo Contable

Por cada factura/documento del Excel, se crea **un asiento contable** tipo `entry`:

```
┌─────────────────────────────────────────────────────────────┐
│ ASIENTO CONTABLE                                            │
├─────────────────────────────────────────────────────────────┤
│ Fecha: fecha de factura original                            │
│ Referencia: MIGLEG/[hash] | Suc: [sucursal] | F/V A 0001-X │
├───────────────────┬───────────────┬─────────────────────────┤
│ Cuenta            │ Debe          │ Haber                   │
├───────────────────┼───────────────┼─────────────────────────┤
│ Deudores por Vta  │ $50,000       │                         │
│  (con partner +   │               │                         │
│   date_maturity)  │               │                         │
├───────────────────┼───────────────┼─────────────────────────┤
│ Resultados Acum.  │               │ $50,000                 │
│  (contrapartida)  │               │                         │
└───────────────────┴───────────────┴─────────────────────────┘
```

## 🚀 Uso

### 1. Solo analizar el Excel (sin conectar a Odoo)

```bash
python migrate_legacy_balances.py --excel /path/to/saldos.xlsx --parse-only
```

### 2. Modo dry-run (simula la migración)

```bash
python migrate_legacy_balances.py --excel /path/to/saldos.xlsx --dry-run
```

### 3. Ejecutar migración real

```bash
python migrate_legacy_balances.py --excel /path/to/saldos.xlsx --execute
```

### 4. Con configuración personalizada

```bash
python migrate_legacy_balances.py \
    --excel /path/to/saldos.xlsx \
    --receivable-account "1.1.3.01.001" \
    --counterpart-account "3.1.1.01.001" \
    --journal "MISC" \
    --migration-date "2026-01-31" \
    --auto-post \
    --execute
```

## ⚙️ Configuración

### Variables de entorno

```bash
export ODOO_URL="https://tu-odoo.com"
export ODOO_DB="produccion"
export ODOO_USER="usuario@ejemplo.com"
export ODOO_PASSWORD="tu_password"
```

### Argumentos CLI

| Argumento | Descripción | Default |
|-----------|-------------|---------|
| `--excel`, `-e` | Ruta al archivo Excel | *Requerido* |
| `--parse-only`, `-p` | Solo analizar, no migrar | - |
| `--dry-run`, `-d` | Simular migración | - |
| `--execute`, `-x` | Ejecutar migración real | - |
| `--receivable-account` | Código cuenta a cobrar | `1.1.3.01.001` |
| `--counterpart-account` | Código cuenta contrapartida | `3.1.1.01.001` |
| `--journal` | Código del diario | `MISC` |
| `--migration-date` | Fecha de migración (YYYY-MM-DD) | Hoy |
| `--auto-post` | Publicar asientos automáticamente | No |
| `--verbose`, `-v` | Mostrar más detalles | No |

## 📄 Formato del Excel Esperado

El script detecta dinámicamente la estructura, pero espera un Excel típico de reporte de cuentas corrientes:

```
┌─────────────────────────────────────────────────────────────┐
│ Saldos de Clientes pendientes de cobro    │ 2026-01-28     │
├─────────────────────────────────────────────────────────────┤
│ EMPRESA SRL                                                 │
├─────────────────────────────────────────────────────────────┤
│ TC  │ L │ Boca │ Nro   │ Cuota│ Fec.Fac │ Venc   │ Pend.  │
├─────────────────────────────────────────────────────────────┤
│ Sucursal: Casa Central                                      │
├─────────────────────────────────────────────────────────────┤
│ Cuenta: 001 CLIENTE EJEMPLO SA                              │
│ Contacto: +54 11 1234-5678                                  │
├─────┼───┼──────┼───────┼──────┼─────────┼────────┼─────────┤
│ F/V │ A │ 0001 │ 12345 │ 1    │ 01/2026 │ 02/2026│ 50000  │
│ F/V │ A │ 0001 │ 12346 │ 1    │ 01/2026 │ 03/2026│ 30000  │
├─────────────────────────────────────────────────────────────┤
│ Total: 80000                                                │
└─────────────────────────────────────────────────────────────┘
```

### Patrones detectados automáticamente

- **Sucursal**: Filas que empiezan con `Sucursal:`, `Suc:`, `Local:`
- **Cliente**: Filas que empiezan con `Cuenta:`, `Cliente:`, `Cod:`
- **Contacto**: Filas que empiezan con `Contacto:`, `Tel:`, `Email:`
- **Facturas**: Filas con tipos `F/V`, `FA`, `FB`, `NC`, `ND`, `REC`, etc.
- **Totales**: Filas con `Total`, `Subtotal` (ignoradas)

## ✅ Características

### Tolerancia a formatos irregulares
- ✅ Celdas combinadas
- ✅ Encabezados visuales
- ✅ Filas vacías
- ✅ Múltiples formatos de fecha
- ✅ Montos con formato argentino (punto miles, coma decimal)

### Idempotencia
- El script genera un hash único por cada documento
- Si se ejecuta dos veces, no duplica los asientos
- El hash incluye: cliente, sucursal, tipo, letra, punto de venta, número, cuota, monto

### Creación automática de partners
- Si el cliente no existe en Odoo, se crea automáticamente
- Se busca por: nombre exacto, nombre parcial, referencia/código
- El partner se crea con `customer_rank = 1`

### Logs detallados
- Muestra qué se creó, qué se omitió, qué errores hubo
- Modo verbose (`-v`) para debugging

## 🔧 Requisitos

```bash
pip install openpyxl
```

## 📝 Ejemplo de Ejecución

```
============================================================
PASO 1: ANÁLISIS DEL ARCHIVO EXCEL
============================================================

📊 Archivo:           /path/to/saldos.xlsx
📅 Fecha del reporte: 2026-01-28
🏢 Empresa:           MUNDO LIMPIO SRL
📍 Sucursales:        Sucursal 1, z Deposito 3
👥 Clientes únicos:   15
📄 Filas totales:     250
✅ Facturas válidas:  87
❌ Facturas inválidas:3
💰 Monto total:       $2,450,000.00

📋 MUESTRA DE FACTURAS DETECTADAS:
--------------------------------------------------------------------------------
  ✅ Fila 22: CLIENTE EJEMPLO SA            | F/V A 0003-00001193  | $   42,299.35
  ✅ Fila 41: OTRO CLIENTE SRL              | F/V A 0015-00002536  | $  399,687.39

============================================================
PASO 2: MIGRACIÓN A ODOO
============================================================

2026-01-31 10:30:00 [INFO] Conectando a Odoo: https://ejemplo.com
2026-01-31 10:30:01 [INFO] Conectado exitosamente. UID: 2
2026-01-31 10:30:01 [INFO] Compañía: Mi Empresa (ID: 1)
2026-01-31 10:30:01 [INFO] Diario: Misceláneos (ID: 5)
2026-01-31 10:30:01 [INFO] Cuenta a cobrar: 1.1.3.01.001 - Deudores por Ventas
2026-01-31 10:30:01 [INFO] Cuenta contrapartida: 3.1.1.01.001 - Resultados Acumulados
...

============================================================
RESULTADO DE LA MIGRACIÓN
============================================================
👥 Partners encontrados:     12
👤 Partners creados:         3
📝 Asientos creados:         87
⏭️  Asientos omitidos (dup): 0
💰 Monto total migrado:      $2,450,000.00

✅ Migración completada exitosamente
```

## 🔮 Extensiones Futuras

1. **Soporte multi-moneda**: Agregar columnas de moneda y monto en moneda extranjera
2. **Proveedores**: Extender para migrar cuentas a pagar
3. **Validación CUIT**: Buscar partners por CUIT/VAT además de nombre
4. **Conciliación automática**: Marcar saldos migrados para conciliación futura
5. **Rollback**: Agregar opción para deshacer una migración por fecha/lote

## ⚠️ Importante

- ❌ **NO** crea facturas fiscales
- ❌ **NO** usa AFIP/CAE/IVA
- ❌ **NO** recalcula impuestos
- ✅ Solo crea asientos contables de apertura
- ✅ Los saldos aparecen en cuentas corrientes
- ✅ Los vencimientos permiten conciliar pagos futuros

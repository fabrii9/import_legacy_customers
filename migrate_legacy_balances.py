#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
MIGRACIÓN DE SALDOS DE CUENTAS CORRIENTES LEGACY A ODOO
================================================================================

Este script migra saldos pendientes de clientes desde un Excel de reporte
contable legacy hacia Odoo, creando asientos contables de apertura.

CARACTERÍSTICAS:
- Detecta dinámicamente clientes, facturas, fechas y montos
- Tolera encabezados, filas vacías, celdas combinadas, estructuras irregulares
- Crea partners automáticamente si no existen
- Genera asientos contables (NO facturas)
- Es idempotente (no duplica si se ejecuta dos veces)
- Soporta modo dry-run para validación
- Genera logs detallados

MODELO CONTABLE:
- Un asiento tipo 'entry' por cada factura legacy
- Línea 1: Cuenta a cobrar + partner + vencimiento (Debe)
- Línea 2: Cuenta de contrapartida (Haber)

USO:
    python migrate_legacy_balances.py --excel /path/to/saldos.xlsx --dry-run
    python migrate_legacy_balances.py --excel /path/to/saldos.xlsx --execute

AUTOR: Generado para Mundo Limpio - Migración Odoo 18
FECHA: 2026-01-31
================================================================================
"""

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple
import hashlib

# =============================================================================
# CONFIGURACIÓN
# =============================================================================

# Odoo connection (pueden sobrescribirse con variables de entorno)
ODOO_URL = os.getenv("ODOO_URL", "https://mundolimpio.aftermoves.com")
ODOO_DB = os.getenv("ODOO_DB", "Testing")
ODOO_USER = os.getenv("ODOO_USER", "fabriziodominguez@aftermoves.com")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "admin")

# Cuentas contables por defecto (pueden sobrescribirse con argumentos)
DEFAULT_RECEIVABLE_ACCOUNT_CODE = "1.1.3.01.001"  # Deudores por Ventas
DEFAULT_COUNTERPART_ACCOUNT_CODE = "3.1.1.01.001"  # Resultados Acumulados / Ajuste Apertura

# Diario por defecto
DEFAULT_JOURNAL_CODE = "MISC"  # Diario Misceláneos

# Prefijo para referencias de asientos (para idempotencia)
MOVE_REF_PREFIX = "MIGLEG"

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class LegacyInvoice:
    """Representa una factura/documento del sistema legacy"""
    # Identificación
    row_number: int
    unique_hash: str = ""
    
    # Cliente
    customer_code: str = ""
    customer_name: str = ""
    customer_contact: str = ""
    
    # Sucursal legacy
    branch_name: str = ""
    
    # Documento
    doc_type: str = ""  # F/V, NC, etc.
    doc_letter: str = ""  # A, B, C
    point_of_sale: str = ""
    doc_number: str = ""
    installment: str = ""  # Cuota
    
    # Fechas
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    
    # Montos
    original_amount: float = 0.0
    pending_amount: float = 0.0
    
    # Mora
    days_overdue: int = 0
    
    # Observaciones
    observations: str = ""
    
    # Procesamiento
    is_valid: bool = True
    validation_errors: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        """Genera hash único para idempotencia"""
        self._regenerate_hash()
    
    def _regenerate_hash(self):
        """Regenera el hash con los datos actuales"""
        # Incluir sucursal en el hash para diferenciar misma factura en diferentes sucursales
        hash_input = f"{self.customer_code}|{self.customer_name}|{self.branch_name}|{self.doc_type}|{self.doc_letter}|{self.point_of_sale}|{self.doc_number}|{self.installment}|{self.pending_amount}"
        self.unique_hash = hashlib.md5(hash_input.encode()).hexdigest()[:12]
    
    @property
    def document_reference(self) -> str:
        """Genera referencia legible del documento"""
        parts = []
        if self.doc_type:
            parts.append(self.doc_type)
        if self.doc_letter:
            parts.append(self.doc_letter)
        if self.point_of_sale and self.doc_number:
            # Limpiar .0 de floats parseados como string
            pos_clean = str(self.point_of_sale).replace('.0', '')
            num_clean = str(self.doc_number).replace('.0', '')
            pos = pos_clean.zfill(4) if pos_clean else "0000"
            num = num_clean.zfill(8) if num_clean else "00000000"
            parts.append(f"{pos}-{num}")
        if self.installment and str(self.installment).replace('.0', '') != "1":
            parts.append(f"Cuota {str(self.installment).replace('.0', '')}")
        return " ".join(parts) if parts else "Saldo Inicial"
    
    @property
    def full_reference(self) -> str:
        """Referencia completa para el asiento"""
        ref = f"{MOVE_REF_PREFIX}/{self.unique_hash}"
        if self.branch_name:
            ref += f" | Suc: {self.branch_name}"
        ref += f" | {self.document_reference}"
        return ref


@dataclass
class ParseResult:
    """Resultado del parsing del Excel"""
    invoices: List[LegacyInvoice] = field(default_factory=list)
    total_rows: int = 0
    valid_invoices: int = 0
    invalid_invoices: int = 0
    total_amount: float = 0.0
    report_date: Optional[date] = None
    company_name: str = ""
    branches: List[str] = field(default_factory=list)
    customers: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class MigrationResult:
    """Resultado de la migración"""
    dry_run: bool = True
    partners_created: int = 0
    partners_found: int = 0
    moves_created: int = 0
    moves_skipped: int = 0  # Por idempotencia
    total_amount_migrated: float = 0.0
    errors: List[str] = field(default_factory=list)
    created_move_ids: List[int] = field(default_factory=list)
    log_entries: List[str] = field(default_factory=list)


# =============================================================================
# PARSER DE EXCEL LEGACY
# =============================================================================

class LegacyExcelParser:
    """
    Parser inteligente para Excel de reportes legacy.
    
    Detecta dinámicamente la estructura basándose en patrones típicos:
    - Filas que empiezan con "Sucursal:" marcan inicio de sección de sucursal
    - Filas que empiezan con "Cuenta:" contienen datos del cliente
    - Filas que empiezan con "Contacto:" tienen info de contacto
    - Filas con tipo de comprobante (F/V, NC, ND, etc.) son líneas de factura
    - Filas con "Total" son subtotales a ignorar
    """
    
    # Patrones de detección
    BRANCH_PATTERNS = [r'^sucursal[:\s]', r'^suc[:\s]', r'^local[:\s]']
    CUSTOMER_PATTERNS = [r'^cuenta[:\s]', r'^cliente[:\s]', r'^cod[:\s]']
    CONTACT_PATTERNS = [r'^contacto[:\s]', r'^tel[:\s]', r'^email[:\s]']
    TOTAL_PATTERNS = [r'^total\s', r'^subtotal\s', r'^total$']
    HEADER_PATTERNS = [r'^tc\s', r'^tipo\s', r'^comprobante', r'^monto']
    
    # Tipos de documento válidos
    DOC_TYPES = ['F/V', 'FV', 'FA', 'FB', 'FC', 'NC', 'ND', 'REC', 'RBO', 'FCE', 'NCE', 'NDE',
                 'FACT', 'FACTURA', 'NOTA DE CREDITO', 'NOTA DE DEBITO', 'RECIBO']
    
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.rows: List[List[Any]] = []
        self.result = ParseResult()
        
    def parse(self) -> ParseResult:
        """Ejecuta el parsing completo del Excel"""
        logger.info(f"Parseando archivo: {self.file_path}")
        
        try:
            self._load_excel()
            self._detect_structure()
            self._extract_invoices()
            self._validate_results()
        except Exception as e:
            logger.exception("Error durante el parsing")
            self.result.errors.append(f"Error crítico: {str(e)}")
        
        return self.result
    
    def _load_excel(self):
        """Carga el Excel usando openpyxl"""
        try:
            import openpyxl
        except ImportError:
            raise ImportError("Instale openpyxl: pip install openpyxl")
        
        wb = openpyxl.load_workbook(self.file_path, read_only=True, data_only=True)
        sheet = wb.active
        
        self.rows = []
        for row in sheet.iter_rows(values_only=True):
            # Convertir a lista de strings limpios
            clean_row = []
            for cell in row:
                if cell is None:
                    clean_row.append("")
                elif isinstance(cell, datetime):
                    clean_row.append(cell)
                else:
                    clean_row.append(str(cell).strip())
            self.rows.append(clean_row)
        
        self.result.total_rows = len(self.rows)
        wb.close()
        logger.info(f"Cargadas {self.result.total_rows} filas")
    
    def _detect_structure(self):
        """Detecta información general del reporte"""
        for idx, row in enumerate(self.rows[:20]):
            row_text = " ".join(str(c) for c in row if c).lower()
            
            # Detectar fecha del reporte
            if 'saldo' in row_text and self.result.report_date is None:
                for cell in row:
                    parsed_date = self._parse_date(cell)
                    if parsed_date:
                        self.result.report_date = parsed_date
                        logger.info(f"Fecha de reporte detectada: {parsed_date}")
                        break
            
            # Detectar nombre de empresa (suele estar en primeras filas, en mayúsculas)
            if not self.result.company_name and idx < 10:
                for cell in row:
                    if cell and isinstance(cell, str) and len(cell) > 5:
                        # Buscar texto que parezca nombre de empresa
                        if cell.isupper() or (cell[0].isupper() and 'S.A' in cell.upper() or 'SRL' in cell.upper() or 'S.R.L' in cell.upper()):
                            if not any(p in cell.lower() for p in ['saldo', 'fecha', 'cuenta', 'cliente']):
                                self.result.company_name = cell
                                logger.info(f"Empresa detectada: {cell}")
                                break
    
    def _extract_invoices(self):
        """Extrae las facturas del Excel"""
        current_branch = ""
        current_customer_code = ""
        current_customer_name = ""
        current_customer_contact = ""
        
        header_row_idx = None
        column_map = {}
        
        for idx, row in enumerate(self.rows):
            if not row or not any(row):
                continue
            
            first_cell = str(row[0]).strip().lower() if row[0] else ""
            
            # ¿Es fila de encabezados?
            if self._is_header_row(row):
                header_row_idx = idx
                column_map = self._build_column_map(row)
                logger.debug(f"Encabezados detectados en fila {idx + 1}: {column_map}")
                continue
            
            # ¿Es fila de sucursal?
            if self._matches_pattern(first_cell, self.BRANCH_PATTERNS):
                current_branch = self._extract_value_after_colon(row)
                if current_branch and current_branch not in self.result.branches:
                    self.result.branches.append(current_branch)
                logger.debug(f"Sucursal: {current_branch}")
                continue
            
            # ¿Es fila de cliente/cuenta?
            if self._matches_pattern(first_cell, self.CUSTOMER_PATTERNS):
                # Formato típico: ['Cuenta:', '20.0', 'PORTAL DEL IGUAZU S.A.', '498200.0', ...]
                current_customer_code = str(row[1]).strip() if len(row) > 1 and row[1] else ""
                current_customer_name = str(row[2]).strip() if len(row) > 2 and row[2] else ""
                # A veces el código está en otra columna
                if len(row) > 3 and row[3]:
                    # Podría ser el código de cuenta
                    pass
                if current_customer_name and current_customer_name not in self.result.customers:
                    self.result.customers.append(current_customer_name)
                logger.debug(f"Cliente: {current_customer_code} - {current_customer_name}")
                continue
            
            # ¿Es fila de contacto?
            if self._matches_pattern(first_cell, self.CONTACT_PATTERNS):
                current_customer_contact = self._extract_value_after_colon(row)
                continue
            
            # ¿Es fila de total? (ignorar)
            if self._matches_pattern(first_cell, self.TOTAL_PATTERNS):
                continue
            
            # ¿Es línea de factura?
            if self._is_invoice_row(row):
                invoice = self._parse_invoice_row(
                    row, 
                    idx + 1,
                    column_map,
                    current_branch,
                    current_customer_code,
                    current_customer_name,
                    current_customer_contact
                )
                if invoice:
                    self.result.invoices.append(invoice)
                    if invoice.is_valid:
                        self.result.valid_invoices += 1
                        self.result.total_amount += invoice.pending_amount
                    else:
                        self.result.invalid_invoices += 1
    
    def _is_header_row(self, row: List) -> bool:
        """Detecta si es fila de encabezados"""
        row_text = " ".join(str(c).lower() for c in row[:5] if c)
        return any(re.search(p, row_text) for p in self.HEADER_PATTERNS)
    
    def _build_column_map(self, row: List) -> Dict[str, int]:
        """Construye mapa de columnas basado en encabezados"""
        column_map = {}
        keywords = {
            'tc': ['tc', 'tipo', 'comp'],
            'letter': ['l', 'letra'],
            'pos': ['boca', 'pto', 'punto', 'suc'],
            'number': ['num', 'nro', 'número'],
            'installment': ['cuota', 'cta'],
            'invoice_date': ['fec. fac', 'fecha fac', 'fec fac', 'f. emision'],
            'observations': ['obs', 'observ'],
            'due_date': ['venc', 'vto', 'f. venc'],
            'original': ['original', 'importe', 'monto'],
            'pending': ['pendiente', 'saldo', 'adeuda'],
            'overdue': ['mora', 'dias'],
        }
        
        for idx, cell in enumerate(row):
            cell_lower = str(cell).lower().strip() if cell else ""
            for key, patterns in keywords.items():
                if any(p in cell_lower for p in patterns):
                    if key not in column_map:  # Primera coincidencia
                        column_map[key] = idx
        
        return column_map
    
    def _is_invoice_row(self, row: List) -> bool:
        """Detecta si la fila contiene datos de factura"""
        if not row or len(row) < 5:
            return False
        
        first_cell = str(row[0]).strip().upper() if row[0] else ""
        
        # Verificar si empieza con tipo de documento conocido
        for doc_type in self.DOC_TYPES:
            if first_cell.startswith(doc_type) or first_cell == doc_type:
                return True
        
        # Verificar si hay montos numéricos en posiciones típicas
        has_numbers = False
        for cell in row[5:11]:
            if self._parse_amount(cell) is not None:
                has_numbers = True
                break
        
        # Si tiene tipo documento parcial y números, es factura
        if has_numbers and len(first_cell) <= 4 and first_cell and first_cell[0].isalpha():
            return True
        
        return False
    
    def _parse_invoice_row(
        self, 
        row: List, 
        row_number: int,
        column_map: Dict[str, int],
        branch: str,
        customer_code: str,
        customer_name: str,
        customer_contact: str
    ) -> Optional[LegacyInvoice]:
        """Parsea una fila de factura"""
        
        invoice = LegacyInvoice(row_number=row_number)
        invoice.branch_name = branch
        invoice.customer_code = customer_code
        invoice.customer_name = customer_name
        invoice.customer_contact = customer_contact
        
        # Si no tenemos customer, es inválido
        if not customer_name and not customer_code:
            invoice.is_valid = False
            invoice.validation_errors.append("Sin cliente asociado")
            return invoice
        
        # Extraer campos según column_map o posiciones por defecto
        # Posiciones típicas del Excel analizado:
        # 0: TC (F/V), 1: L (A/B/C), 2: Boca/POS, 3: Número, 4: Cuota
        # 5: Fecha Fac, 6: Obs, 7: Venc, 8: Monto Orig, 9: $, 10: Pendiente, 12: Mora
        
        try:
            # Tipo de comprobante
            invoice.doc_type = str(row[column_map.get('tc', 0)]).strip() if len(row) > 0 else ""
            
            # Letra
            invoice.doc_letter = str(row[column_map.get('letter', 1)]).strip() if len(row) > 1 else ""
            
            # Punto de venta
            pos_val = row[column_map.get('pos', 2)] if len(row) > 2 else ""
            invoice.point_of_sale = self._clean_number_string(pos_val)
            
            # Número de documento
            num_val = row[column_map.get('number', 3)] if len(row) > 3 else ""
            invoice.doc_number = self._clean_number_string(num_val)
            
            # Cuota
            inst_val = row[column_map.get('installment', 4)] if len(row) > 4 else ""
            invoice.installment = self._clean_number_string(inst_val)
            
            # Fecha de factura
            invoice.invoice_date = self._parse_date(row[column_map.get('invoice_date', 5)] if len(row) > 5 else None)
            
            # Observaciones
            invoice.observations = str(row[column_map.get('observations', 6)]).strip() if len(row) > 6 else ""
            
            # Fecha de vencimiento
            invoice.due_date = self._parse_date(row[column_map.get('due_date', 7)] if len(row) > 7 else None)
            
            # Monto original - buscar en posición 8 o donde haya número
            orig_amount = self._parse_amount(row[column_map.get('original', 8)] if len(row) > 8 else None)
            if orig_amount is not None:
                invoice.original_amount = orig_amount
            
            # Monto pendiente - posición 10 típicamente
            pending_idx = column_map.get('pending', 10)
            pending_amount = self._parse_amount(row[pending_idx] if len(row) > pending_idx else None)
            
            # Si no hay pendiente en la posición esperada, buscar
            if pending_amount is None:
                for idx in [10, 11, 9, 8]:
                    if idx < len(row):
                        pending_amount = self._parse_amount(row[idx])
                        if pending_amount is not None and pending_amount > 0:
                            break
            
            if pending_amount is not None:
                invoice.pending_amount = pending_amount
            else:
                invoice.is_valid = False
                invoice.validation_errors.append("No se pudo determinar monto pendiente")
            
            # Días de mora
            mora_idx = column_map.get('overdue', 12)
            if mora_idx < len(row):
                mora_val = self._parse_amount(row[mora_idx])
                if mora_val is not None:
                    invoice.days_overdue = int(mora_val)
            
        except Exception as e:
            invoice.is_valid = False
            invoice.validation_errors.append(f"Error parseando: {str(e)}")
        
        # Validaciones adicionales
        if invoice.pending_amount <= 0:
            invoice.is_valid = False
            invoice.validation_errors.append(f"Monto pendiente inválido: {invoice.pending_amount}")
        
        # Regenerar hash con datos completos (incluyendo branch_name)
        invoice._regenerate_hash()
        
        return invoice
    
    def _matches_pattern(self, text: str, patterns: List[str]) -> bool:
        """Verifica si el texto coincide con algún patrón"""
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False
    
    def _extract_value_after_colon(self, row: List) -> str:
        """Extrae el valor después de los dos puntos"""
        # Buscar en primera celda después del :
        first_cell = str(row[0]) if row[0] else ""
        if ':' in first_cell:
            parts = first_cell.split(':', 1)
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()
        
        # Buscar en siguiente celda
        if len(row) > 1 and row[1]:
            return str(row[1]).strip()
        
        return ""
    
    def _parse_date(self, value: Any) -> Optional[date]:
        """Parsea una fecha desde varios formatos"""
        if value is None or value == "" or str(value).strip() == "":
            return None
        
        if isinstance(value, datetime):
            return value.date()
        
        if isinstance(value, date):
            return value
        
        date_str = str(value).strip()
        
        # Formatos comunes
        formats = [
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d',
            '%d/%m/%Y',
            '%d-%m-%Y',
            '%d.%m.%Y',
            '%Y/%m/%d',
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        
        return None
    
    def _parse_amount(self, value: Any) -> Optional[float]:
        """Parsea un monto numérico"""
        if value is None:
            return None
        
        if isinstance(value, (int, float)):
            return float(value)
        
        value_str = str(value).strip()
        if not value_str or value_str == '$':
            return None
        
        # Limpiar caracteres de moneda y espacios
        value_str = value_str.replace('$', '').replace(' ', '').replace('\xa0', '')
        
        # Manejar formato argentino (punto miles, coma decimal)
        if ',' in value_str and '.' in value_str:
            if value_str.rfind(',') > value_str.rfind('.'):
                # Coma es decimal
                value_str = value_str.replace('.', '').replace(',', '.')
            else:
                # Punto es decimal
                value_str = value_str.replace(',', '')
        elif ',' in value_str:
            # Solo coma -> es decimal
            value_str = value_str.replace(',', '.')
        
        try:
            return float(value_str)
        except ValueError:
            return None
    
    def _clean_number_string(self, value: Any) -> str:
        """Limpia un valor numérico para usar como string"""
        if value is None:
            return ""
        
        if isinstance(value, float):
            # Quitar .0 si es entero
            if value == int(value):
                return str(int(value))
            return str(value)
        
        return str(value).strip()
    
    def _validate_results(self):
        """Valida y genera warnings sobre los resultados"""
        if not self.result.invoices:
            self.result.errors.append("No se encontraron facturas válidas")
            return
        
        # Verificar duplicados por hash (mismo documento, mismo monto)
        seen_hashes = {}
        for inv in self.result.invoices:
            if inv.unique_hash in seen_hashes:
                prev = seen_hashes[inv.unique_hash]
                self.result.warnings.append(
                    f"Duplicado exacto detectado: Fila {prev.row_number} y Fila {inv.row_number} - "
                    f"{inv.customer_name} - {inv.document_reference} - ${inv.pending_amount:,.2f}"
                )
            seen_hashes[inv.unique_hash] = inv
        
        # Verificar clientes sin nombre
        unnamed = [inv for inv in self.result.invoices if not inv.customer_name]
        if unnamed:
            self.result.warnings.append(
                f"{len(unnamed)} facturas sin nombre de cliente (solo código)"
            )
        
        logger.info(f"Parsing completado: {self.result.valid_invoices} facturas válidas, "
                   f"{self.result.invalid_invoices} inválidas, "
                   f"Total: ${self.result.total_amount:,.2f}")


# =============================================================================
# CLIENTE ODOO (XML-RPC)
# =============================================================================

class OdooClient:
    """Cliente XML-RPC para Odoo"""
    
    def __init__(self, url: str, db: str, user: str, password: str):
        import xmlrpc.client
        
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.password = password
        
        logger.info(f"Conectando a Odoo: {self.url}")
        
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self.uid = common.authenticate(self.db, self.user, self.password, {})
        
        if not self.uid:
            raise RuntimeError(f"Error de autenticación en Odoo. Usuario: {self.user}")
        
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        logger.info(f"Conectado exitosamente. UID: {self.uid}")
    
    def execute_kw(self, model: str, method: str, args: List, kwargs: Optional[Dict] = None):
        """Ejecuta método en Odoo"""
        kwargs = kwargs or {}
        return self.models.execute_kw(
            self.db, self.uid, self.password, model, method, args, kwargs
        )
    
    def search(self, model: str, domain: List, limit: int = 0) -> List[int]:
        """Busca registros"""
        opts = {"limit": limit} if limit else {}
        return self.execute_kw(model, "search", [domain], opts)
    
    def search_read(self, model: str, domain: List, fields: List[str], limit: int = 0) -> List[Dict]:
        """Busca y lee registros"""
        opts = {"fields": fields}
        if limit:
            opts["limit"] = limit
        return self.execute_kw(model, "search_read", [domain], opts)
    
    def read(self, model: str, ids: List[int], fields: List[str]) -> List[Dict]:
        """Lee registros por IDs"""
        return self.execute_kw(model, "read", [ids, fields])
    
    def create(self, model: str, vals: Dict) -> int:
        """Crea un registro"""
        return self.execute_kw(model, "create", [vals])
    
    def write(self, model: str, ids: List[int], vals: Dict) -> bool:
        """Actualiza registros"""
        return self.execute_kw(model, "write", [ids, vals])


# =============================================================================
# MIGRADOR DE SALDOS
# =============================================================================

class LegacyBalanceMigrator:
    """
    Migrador de saldos legacy a Odoo.
    
    Crea asientos contables de tipo 'entry' para migrar cuentas corrientes
    sin crear facturas fiscales.
    """
    
    def __init__(
        self,
        client: OdooClient,
        receivable_account_code: str = DEFAULT_RECEIVABLE_ACCOUNT_CODE,
        counterpart_account_code: str = DEFAULT_COUNTERPART_ACCOUNT_CODE,
        journal_code: str = DEFAULT_JOURNAL_CODE,
        migration_date: Optional[date] = None,
        company_id: Optional[int] = None,
        dry_run: bool = True,
        auto_post: bool = False
    ):
        self.client = client
        self.receivable_account_code = receivable_account_code
        self.counterpart_account_code = counterpart_account_code
        self.journal_code = journal_code
        self.migration_date = migration_date or date.today()
        self.dry_run = dry_run
        self.auto_post = auto_post
        
        # Cache
        self._company_id = company_id
        self._journal_id: Optional[int] = None
        self._receivable_account_id: Optional[int] = None
        self._counterpart_account_id: Optional[int] = None
        self._partner_cache: Dict[str, int] = {}
        self._existing_moves: Dict[str, int] = {}
        
        self.result = MigrationResult(dry_run=dry_run)
    
    def migrate(self, invoices: List[LegacyInvoice]) -> MigrationResult:
        """Ejecuta la migración de las facturas legacy"""
        logger.info(f"{'[DRY-RUN] ' if self.dry_run else ''}Iniciando migración de {len(invoices)} facturas")
        
        try:
            # Inicializar
            self._init_company()
            self._init_journal()
            self._init_accounts()
            self._load_existing_moves()
            
            # Procesar cada factura
            for invoice in invoices:
                if not invoice.is_valid:
                    self._log(f"SKIP (inválida): Fila {invoice.row_number} - {invoice.validation_errors}")
                    continue
                
                self._process_invoice(invoice)
            
            # Resumen
            self._log_summary()
            
        except Exception as e:
            logger.exception("Error durante la migración")
            self.result.errors.append(f"Error crítico: {str(e)}")
        
        return self.result
    
    def _init_company(self):
        """Obtiene o valida la compañía"""
        if self._company_id:
            return
        
        companies = self.client.search_read(
            "res.company", [], ["id", "name"], limit=1
        )
        if companies:
            self._company_id = companies[0]["id"]
            logger.info(f"Compañía: {companies[0]['name']} (ID: {self._company_id})")
        else:
            raise RuntimeError("No se encontró ninguna compañía en Odoo")
    
    def _init_journal(self):
        """Obtiene el diario para los asientos"""
        journals = self.client.search_read(
            "account.journal",
            [("code", "=", self.journal_code), ("company_id", "=", self._company_id)],
            ["id", "name", "type"]
        )
        
        if not journals:
            # Buscar cualquier diario general
            journals = self.client.search_read(
                "account.journal",
                [("type", "=", "general"), ("company_id", "=", self._company_id)],
                ["id", "name", "code"],
                limit=1
            )
        
        if journals:
            self._journal_id = journals[0]["id"]
            logger.info(f"Diario: {journals[0].get('name', '')} (ID: {self._journal_id})")
        else:
            raise RuntimeError(f"No se encontró diario con código '{self.journal_code}' ni diario general")
    
    def _init_accounts(self):
        """Obtiene las cuentas contables"""
        # Cuenta a cobrar - En Odoo 18 account.account no tiene company_id directo
        accounts = self.client.search_read(
            "account.account",
            [("code", "=", self.receivable_account_code)],
            ["id", "name", "code"]
        )
        
        if not accounts:
            # Buscar cuenta receivable por defecto
            accounts = self.client.search_read(
                "account.account",
                [("account_type", "=", "asset_receivable")],
                ["id", "name", "code"],
                limit=1
            )
        
        if accounts:
            self._receivable_account_id = accounts[0]["id"]
            logger.info(f"Cuenta a cobrar: {accounts[0]['code']} - {accounts[0]['name']}")
        else:
            raise RuntimeError(f"No se encontró cuenta a cobrar (código: {self.receivable_account_code})")
        
        # Cuenta de contrapartida
        accounts = self.client.search_read(
            "account.account",
            [("code", "=", self.counterpart_account_code)],
            ["id", "name", "code"]
        )
        
        if not accounts:
            # Buscar cuenta de equity/ajuste
            accounts = self.client.search_read(
                "account.account",
                [("account_type", "in", ["equity", "equity_unaffected"])],
                ["id", "name", "code"],
                limit=1
            )
        
        if accounts:
            self._counterpart_account_id = accounts[0]["id"]
            logger.info(f"Cuenta contrapartida: {accounts[0]['code']} - {accounts[0]['name']}")
        else:
            raise RuntimeError(f"No se encontró cuenta de contrapartida (código: {self.counterpart_account_code})")
    
    def _load_existing_moves(self):
        """Carga asientos existentes para idempotencia"""
        moves = self.client.search_read(
            "account.move",
            [("ref", "like", f"{MOVE_REF_PREFIX}/"), ("company_id", "=", self._company_id)],
            ["id", "ref"]
        )
        
        for move in moves:
            ref = move.get("ref", "")
            # Extraer hash de la referencia
            if f"{MOVE_REF_PREFIX}/" in ref:
                hash_part = ref.split(f"{MOVE_REF_PREFIX}/")[1].split()[0].split("|")[0].strip()
                self._existing_moves[hash_part] = move["id"]
        
        logger.info(f"Asientos de migración existentes: {len(self._existing_moves)}")
    
    def _get_or_create_partner(self, invoice: LegacyInvoice) -> Optional[int]:
        """Obtiene o crea el partner"""
        # Clave de cache: nombre + código
        cache_key = f"{invoice.customer_name}|{invoice.customer_code}"
        
        if cache_key in self._partner_cache:
            return self._partner_cache[cache_key]
        
        partner_id = None
        
        # Buscar por nombre exacto
        if invoice.customer_name:
            partners = self.client.search(
                "res.partner",
                [("name", "=", invoice.customer_name)],
                limit=1
            )
            if partners:
                partner_id = partners[0]
        
        # Buscar por nombre parcial
        if not partner_id and invoice.customer_name:
            partners = self.client.search(
                "res.partner",
                [("name", "ilike", invoice.customer_name)],
                limit=1
            )
            if partners:
                partner_id = partners[0]
        
        # Buscar por referencia/código
        if not partner_id and invoice.customer_code:
            partners = self.client.search(
                "res.partner",
                [("ref", "=", invoice.customer_code)],
                limit=1
            )
            if partners:
                partner_id = partners[0]
        
        # Crear si no existe
        if not partner_id:
            if self.dry_run:
                self._log(f"[DRY-RUN] Crearía partner: {invoice.customer_name or invoice.customer_code}")
                self.result.partners_created += 1
                # Retornar ID ficticio para dry-run
                partner_id = -1
            else:
                vals = {
                    "name": invoice.customer_name or f"Cliente {invoice.customer_code}",
                    "customer_rank": 1,
                    "company_id": False,  # Partner compartido
                }
                if invoice.customer_code:
                    vals["ref"] = invoice.customer_code
                if invoice.customer_contact:
                    # Intentar extraer teléfono
                    vals["comment"] = f"Migrado del sistema legacy. Contacto: {invoice.customer_contact}"
                
                partner_id = self.client.create("res.partner", vals)
                self._log(f"Partner creado: {vals['name']} (ID: {partner_id})")
                self.result.partners_created += 1
        else:
            self.result.partners_found += 1
        
        self._partner_cache[cache_key] = partner_id
        return partner_id
    
    def _process_invoice(self, invoice: LegacyInvoice):
        """Procesa una factura y crea el asiento"""
        
        # Verificar idempotencia
        if invoice.unique_hash in self._existing_moves:
            self._log(f"SKIP (existe): {invoice.customer_name} - {invoice.document_reference}")
            self.result.moves_skipped += 1
            return
        
        # Obtener partner
        partner_id = self._get_or_create_partner(invoice)
        if not partner_id:
            self.result.errors.append(
                f"Fila {invoice.row_number}: No se pudo obtener/crear partner para {invoice.customer_name}"
            )
            return
        
        # Preparar asiento
        move_date = invoice.invoice_date or self.migration_date
        
        # Línea del cliente (Debe - cuenta a cobrar)
        line_receivable = {
            "account_id": self._receivable_account_id,
            "partner_id": partner_id,
            "name": invoice.document_reference,
            "debit": invoice.pending_amount,
            "credit": 0.0,
        }
        
        # Agregar fecha de vencimiento si existe
        if invoice.due_date:
            line_receivable["date_maturity"] = invoice.due_date.isoformat()
        
        # Línea de contrapartida (Haber)
        line_counterpart = {
            "account_id": self._counterpart_account_id,
            "partner_id": False,
            "name": f"Contrapartida migración - {invoice.customer_name}",
            "debit": 0.0,
            "credit": invoice.pending_amount,
        }
        
        move_vals = {
            "journal_id": self._journal_id,
            "date": move_date.isoformat(),
            "ref": invoice.full_reference,
            "company_id": self._company_id,
            "move_type": "entry",
            "line_ids": [
                (0, 0, line_receivable),
                (0, 0, line_counterpart),
            ],
        }
        
        # Agregar narración con detalles
        narration_parts = [
            "=== MIGRACIÓN SISTEMA LEGACY ===",
            f"Cliente: {invoice.customer_name}",
            f"Código: {invoice.customer_code}",
            f"Sucursal legacy: {invoice.branch_name}",
            f"Documento: {invoice.document_reference}",
            f"Monto original: ${invoice.original_amount:,.2f}",
            f"Monto pendiente: ${invoice.pending_amount:,.2f}",
        ]
        if invoice.due_date:
            narration_parts.append(f"Vencimiento: {invoice.due_date}")
        if invoice.days_overdue:
            narration_parts.append(f"Días de mora: {invoice.days_overdue}")
        if invoice.observations:
            narration_parts.append(f"Observaciones: {invoice.observations}")
        
        move_vals["narration"] = "\n".join(narration_parts)
        
        if self.dry_run:
            self._log(
                f"[DRY-RUN] Crearía asiento: {invoice.customer_name} - "
                f"{invoice.document_reference} - ${invoice.pending_amount:,.2f}"
            )
            self.result.moves_created += 1
            self.result.total_amount_migrated += invoice.pending_amount
        else:
            try:
                move_id = self.client.create("account.move", move_vals)
                self.result.created_move_ids.append(move_id)
                self.result.moves_created += 1
                self.result.total_amount_migrated += invoice.pending_amount
                
                self._log(
                    f"Asiento creado (ID: {move_id}): {invoice.customer_name} - "
                    f"{invoice.document_reference} - ${invoice.pending_amount:,.2f}"
                )
                
                # Publicar si corresponde
                if self.auto_post:
                    try:
                        self.client.execute_kw("account.move", "action_post", [[move_id]])
                    except Exception as e:
                        self.result.errors.append(f"Error publicando asiento {move_id}: {str(e)}")
                
            except Exception as e:
                self.result.errors.append(
                    f"Error creando asiento para {invoice.customer_name}: {str(e)}"
                )
    
    def _log(self, message: str):
        """Registra mensaje en log y resultado"""
        logger.info(message)
        self.result.log_entries.append(message)
    
    def _log_summary(self):
        """Registra resumen de la migración"""
        summary = [
            "",
            "=" * 60,
            f"{'[DRY-RUN] ' if self.dry_run else ''}RESUMEN DE MIGRACIÓN",
            "=" * 60,
            f"Partners encontrados:     {self.result.partners_found}",
            f"Partners creados:         {self.result.partners_created}",
            f"Asientos creados:         {self.result.moves_created}",
            f"Asientos omitidos (dup):  {self.result.moves_skipped}",
            f"Monto total migrado:      ${self.result.total_amount_migrated:,.2f}",
            f"Errores:                  {len(self.result.errors)}",
            "=" * 60,
        ]
        
        for line in summary:
            logger.info(line)
            self.result.log_entries.append(line)
        
        if self.result.errors:
            logger.warning("ERRORES ENCONTRADOS:")
            for error in self.result.errors:
                logger.warning(f"  - {error}")


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Migración de saldos de cuentas corrientes legacy a Odoo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
  # Solo analizar el Excel (sin conectar a Odoo)
  python migrate_legacy_balances.py --excel /path/to/saldos.xlsx --parse-only
  
  # Modo dry-run (simula la migración)
  python migrate_legacy_balances.py --excel /path/to/saldos.xlsx --dry-run
  
  # Ejecutar migración real
  python migrate_legacy_balances.py --excel /path/to/saldos.xlsx --execute
  
  # Con cuentas personalizadas
  python migrate_legacy_balances.py --excel /path/to/saldos.xlsx \\
    --receivable-account 1.1.3.01.001 \\
    --counterpart-account 3.1.1.01.001 \\
    --execute

Variables de entorno:
  ODOO_URL      - URL del servidor Odoo
  ODOO_DB       - Nombre de la base de datos
  ODOO_USER     - Usuario de Odoo
  ODOO_PASSWORD - Contraseña de Odoo
        """
    )
    
    parser.add_argument(
        "--excel", "-e",
        required=True,
        help="Ruta al archivo Excel de saldos legacy"
    )
    
    parser.add_argument(
        "--parse-only", "-p",
        action="store_true",
        help="Solo parsear el Excel sin conectar a Odoo"
    )
    
    parser.add_argument(
        "--dry-run", "-d",
        action="store_true",
        help="Simular la migración sin crear registros"
    )
    
    parser.add_argument(
        "--execute", "-x",
        action="store_true",
        help="Ejecutar la migración real"
    )
    
    parser.add_argument(
        "--auto-post",
        action="store_true",
        help="Publicar automáticamente los asientos creados"
    )
    
    parser.add_argument(
        "--receivable-account",
        default=DEFAULT_RECEIVABLE_ACCOUNT_CODE,
        help=f"Código de cuenta a cobrar (default: {DEFAULT_RECEIVABLE_ACCOUNT_CODE})"
    )
    
    parser.add_argument(
        "--counterpart-account",
        default=DEFAULT_COUNTERPART_ACCOUNT_CODE,
        help=f"Código de cuenta de contrapartida (default: {DEFAULT_COUNTERPART_ACCOUNT_CODE})"
    )
    
    parser.add_argument(
        "--journal",
        default=DEFAULT_JOURNAL_CODE,
        help=f"Código del diario (default: {DEFAULT_JOURNAL_CODE})"
    )
    
    parser.add_argument(
        "--migration-date",
        help="Fecha de migración (YYYY-MM-DD). Default: hoy"
    )
    
    parser.add_argument(
        "--url",
        default=ODOO_URL,
        help="URL del servidor Odoo"
    )
    
    parser.add_argument(
        "--db",
        default=ODOO_DB,
        help="Nombre de la base de datos Odoo"
    )
    
    parser.add_argument(
        "--user",
        default=ODOO_USER,
        help="Usuario de Odoo"
    )
    
    parser.add_argument(
        "--password",
        default=ODOO_PASSWORD,
        help="Contraseña de Odoo"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Mostrar más detalles"
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Validar argumentos
    if not args.parse_only and not args.dry_run and not args.execute:
        parser.error("Debe especificar --parse-only, --dry-run o --execute")
    
    if not os.path.exists(args.excel):
        parser.error(f"El archivo no existe: {args.excel}")
    
    # 1. Parsear Excel
    print("\n" + "=" * 60)
    print("PASO 1: ANÁLISIS DEL ARCHIVO EXCEL")
    print("=" * 60 + "\n")
    
    parser_obj = LegacyExcelParser(args.excel)
    parse_result = parser_obj.parse()
    
    # Mostrar resultados del parsing
    print(f"\n📊 Archivo:           {args.excel}")
    print(f"📅 Fecha del reporte: {parse_result.report_date or 'No detectada'}")
    print(f"🏢 Empresa:           {parse_result.company_name or 'No detectada'}")
    print(f"📍 Sucursales:        {', '.join(parse_result.branches) if parse_result.branches else 'No detectadas'}")
    print(f"👥 Clientes únicos:   {len(parse_result.customers)}")
    print(f"📄 Filas totales:     {parse_result.total_rows}")
    print(f"✅ Facturas válidas:  {parse_result.valid_invoices}")
    print(f"❌ Facturas inválidas:{parse_result.invalid_invoices}")
    print(f"💰 Monto total:       ${parse_result.total_amount:,.2f}")
    
    if parse_result.warnings:
        print("\n⚠️  ADVERTENCIAS:")
        for w in parse_result.warnings:
            print(f"   - {w}")
    
    if parse_result.errors:
        print("\n❌ ERRORES:")
        for e in parse_result.errors:
            print(f"   - {e}")
        if not args.parse_only:
            print("\nAbortando debido a errores en el parsing.")
            sys.exit(1)
    
    # Mostrar muestra de facturas
    if parse_result.invoices:
        print("\n📋 MUESTRA DE FACTURAS DETECTADAS:")
        print("-" * 80)
        for inv in parse_result.invoices[:5]:
            status = "✅" if inv.is_valid else "❌"
            print(f"  {status} Fila {inv.row_number}: {inv.customer_name[:30]:<30} | "
                  f"{inv.document_reference:<20} | ${inv.pending_amount:>12,.2f}")
        if len(parse_result.invoices) > 5:
            print(f"  ... y {len(parse_result.invoices) - 5} más")
    
    if args.parse_only:
        print("\n✅ Análisis completado (modo --parse-only)")
        sys.exit(0)
    
    # 2. Migrar a Odoo
    print("\n" + "=" * 60)
    print("PASO 2: MIGRACIÓN A ODOO")
    print("=" * 60 + "\n")
    
    # Parsear fecha de migración
    migration_date = None
    if args.migration_date:
        try:
            migration_date = datetime.strptime(args.migration_date, "%Y-%m-%d").date()
        except ValueError:
            parser.error("Formato de fecha inválido. Use YYYY-MM-DD")
    
    # Conectar a Odoo
    try:
        client = OdooClient(args.url, args.db, args.user, args.password)
    except Exception as e:
        print(f"❌ Error conectando a Odoo: {e}")
        sys.exit(1)
    
    # Ejecutar migración
    migrator = LegacyBalanceMigrator(
        client=client,
        receivable_account_code=args.receivable_account,
        counterpart_account_code=args.counterpart_account,
        journal_code=args.journal,
        migration_date=migration_date,
        dry_run=args.dry_run,
        auto_post=args.auto_post
    )
    
    valid_invoices = [inv for inv in parse_result.invoices if inv.is_valid]
    migration_result = migrator.migrate(valid_invoices)
    
    # Mostrar resultados
    print("\n" + "=" * 60)
    print(f"{'[DRY-RUN] ' if args.dry_run else ''}RESULTADO DE LA MIGRACIÓN")
    print("=" * 60)
    print(f"👥 Partners encontrados:     {migration_result.partners_found}")
    print(f"👤 Partners creados:         {migration_result.partners_created}")
    print(f"📝 Asientos creados:         {migration_result.moves_created}")
    print(f"⏭️  Asientos omitidos (dup): {migration_result.moves_skipped}")
    print(f"💰 Monto total migrado:      ${migration_result.total_amount_migrated:,.2f}")
    
    if migration_result.errors:
        print(f"\n❌ ERRORES ({len(migration_result.errors)}):")
        for e in migration_result.errors[:10]:
            print(f"   - {e}")
        if len(migration_result.errors) > 10:
            print(f"   ... y {len(migration_result.errors) - 10} más")
    
    if args.dry_run:
        print("\n💡 Este fue un DRY-RUN. Para ejecutar realmente, use --execute")
    else:
        print("\n✅ Migración completada exitosamente")
        if migration_result.created_move_ids:
            print(f"   IDs de asientos creados: {migration_result.created_move_ids[:20]}")
            if len(migration_result.created_move_ids) > 20:
                print(f"   ... y {len(migration_result.created_move_ids) - 20} más")


if __name__ == "__main__":
    main()

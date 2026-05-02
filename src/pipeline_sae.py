import pandas as pd
import os
import logging

# Configuración de logging para registrar la ejecución del pipeline
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Función para cargar archivos csv con diferentes separadores evitando errores de lectura.

def cargar_csv_seguro(ruta, usecols=None):
    """Carga archivos CSV intentando múltiples separadores para evitar errores de codificación."""
    separadores = [';', ',', '|']
    
    for sep in separadores:
        try:
            df = pd.read_csv(ruta, sep=sep, encoding='latin-1', usecols=usecols)
            
            if usecols is None and len(df.columns) == 1:
                continue # Prueba con el siguiente separador
                
            return df # Si llegó aquí sin errores, el separador funcionó perfectamente
            
        except ValueError:
            # Separador incorrecto para el usecols pedido, intentamos con el siguiente
            continue 
        except Exception as e:
            logging.error(f"Error al cargar {ruta}: {e}")
            raise
            
    # Si termina el ciclo y no retornó, las columnas de usecols realmente no existen
    raise ValueError(f"No se pudieron encontrar las columnas en {ruta} con los separadores conocidos.")

# Función para el desarrollo del paso n°1 sobre oferta 2019, entrega csv con datos procesados

def generar_oferta_2019(rutas, ruta_salida):
    """Ejecuta el Paso 1: Obtención de la Oferta 2019."""
    logging.info("Iniciando Paso 1: Generación de Oferta 2019...")
    
    df_ficha = cargar_csv_seguro(rutas['ficha'])
    df_log = cargar_csv_seguro(rutas['log'], usecols=['RBD', 'ID_LOG_CUPO', 'FECHA_ULTIMA_ACTUALIZACION', 'CARGADO_VITRINA'])
    df_anexo = cargar_csv_seguro(rutas['anexo'])
    df_admi = cargar_csv_seguro(rutas['admi'])
    df_coles = cargar_csv_seguro(rutas['coles'])

    # Estandarizar columnas a minúsculas
    for df in [df_log, df_anexo, df_coles]:
        df.columns = df.columns.str.lower()

    # Regla 1: Última actualización válida
    log_valido = df_log[df_log['cargado_vitrina'] == 1].copy()
    log_valido['fecha_ultima_actualizacion'] = pd.to_datetime(log_valido['fecha_ultima_actualizacion'])
    idx_max = log_valido.groupby('rbd')['fecha_ultima_actualizacion'].idxmax()
    ids_validos = log_valido.loc[idx_max, 'id_log_cupo'].tolist()
    oferta = df_ficha[df_ficha['id_log_cupo'].isin(ids_validos)].copy()

    # Regla 2: Anexos y sedes
    oferta = oferta.merge(df_anexo[['id_anexo', 'n_correlativo']], left_on='id_sede_anexo', right_on='id_anexo', how='left')
    oferta['cod_sede'] = oferta['n_correlativo'].fillna(1).astype(int)

    # Regla 3: Cupos mayores a 0
    oferta = oferta[oferta['total_cupos'] > 0]

    # Regla 4: Emparejamiento de cursos
    oferta['cod_genero'] = oferta['tipo_alumnado']
    oferta['cod_jor'] = oferta['tipo_jornada']
    oferta['cod_esp'] = oferta['cod_esp'].fillna(0).astype(int)
    df_admi['cod_esp'] = df_admi['cod_esp'].fillna(0).astype(int)

    oferta_final = oferta.merge(
        df_admi[['cod_curso', 'cod_nivel', 'cod_gra', 'cod_ens', 'cod_esp', 'cod_genero', 'cod_jor', 'cod_sede']],
        on=['cod_gra', 'cod_ens', 'cod_esp', 'cod_sede', 'cod_genero', 'cod_jor'],
        how='inner'
    )

    # Regla 5: Filtro oficial SAE
    oferta_final = oferta_final.merge(df_coles[['rbd']], on='rbd', how='inner')
    oferta_final = oferta_final[['rbd', 'cod_nivel', 'cod_curso', 'total_cupos']]
    oferta_final['total_cupos'] = oferta_final['total_cupos'].astype(int)
    oferta_final.to_csv(ruta_salida, index=False)
    logging.info(f"Paso 1 completado. Total cursos: {len(oferta_final)}. Exportado a {ruta_salida}")

# Función para el desarrollo del paso n°2 sobre matrícula asegurada, entrega csv con datos procesados

def generar_estudiantes_preinscribir(rutas, ruta_salida):
    """Ejecuta el Paso 2: Identificación de estudiantes para preinscripción."""
    logging.info("Iniciando Paso 2: Cálculo de Matrícula Asegurada...")

    df_oferta_test = cargar_csv_seguro(rutas['oferta_test'])
    df_fus = cargar_csv_seguro(rutas['fusiones'])
    df_cod = cargar_csv_seguro(rutas['codigos_nivel'])

    cols_mat = ['rbd', 'fecha_incorporacion', 'fecha_retiro', 'cod_ens', 'cod_gra', 'sal_run', 'sal_fec_nac', 'cod_region', 'tipo_dependencia']
    df_mat = cargar_csv_seguro(rutas['matricula'], usecols=cols_mat)

    # Filtros base
    ens_regular = [10, 110, 310, 410, 510, 610, 710, 810, 910]
    df_mat = df_mat[df_mat['cod_ens'].isin(ens_regular)]
    df_mat = df_mat[df_mat['fecha_retiro'].isna() | (df_mat['fecha_retiro'].astype(str).str.strip() == '')]

    # Filtro de edad
    df_mat['sal_fec_nac'] = pd.to_datetime(df_mat['sal_fec_nac'], errors='coerce', dayfirst=True)
    df_mat['edad_2019'] = 2019 - df_mat['sal_fec_nac'].dt.year - (df_mat['sal_fec_nac'].dt.month > 3).astype(int)
    df_mat = df_mat[df_mat['edad_2019'] >= 4]

    # Niveles y desduplicación
    df_mat = df_mat.merge(df_cod, on=['cod_ens', 'cod_gra'], how='inner')
    df_mat = df_mat[df_mat['cod_nivel'] >= -1]
    
    df_mat['fecha_incorporacion'] = pd.to_datetime(df_mat['fecha_incorporacion'], errors='coerce', dayfirst=True)
    df_mat = df_mat.sort_values('fecha_incorporacion', ascending=False).drop_duplicates(subset=['sal_run'], keep='first')

    # Mapeo de Continuidad
    df_mat = df_mat.merge(df_fus[['rbd', 'rbd_principal']], on='rbd', how='left')
    df_mat['rbd_2019'] = df_mat['rbd_principal'].fillna(df_mat['rbd']).astype(int)
    df_mat['cod_nivel_2019'] = df_mat['cod_nivel'] + 1

    oferta_pares = df_oferta_test[['rbd', 'cod_nivel']].drop_duplicates()
    oferta_pares.rename(columns={'rbd': 'rbd_2019', 'cod_nivel': 'cod_nivel_2019'}, inplace=True)

    df_final = df_mat.merge(oferta_pares, on=['rbd_2019', 'cod_nivel_2019'], how='inner')
    df_final = df_final[['sal_run', 'rbd_2019', 'cod_nivel_2019']]

    df_final.to_csv(ruta_salida, index=False)
    logging.info(f"Paso 2 completado. Total estudiantes: {len(df_final)}. Exportado a {ruta_salida}")

if __name__ == "__main__":
    # Asegurar que exista el directorio de salida
    os.makedirs('../data/processed', exist_ok=True)

    RUTAS = {
        'ficha': '../data/raw/SIGE_SAE_LOG_FICHA_CUPO_versionTest.csv',
        'log': '../data/raw/SIGE_SAE_LOG_CUPO_versionTest.csv',
        'anexo': '../data/raw/SIGE_SAE_LOG_ANEXO_SEDE_versionTest.csv',
        'admi': '../data/raw/ADMI_CURSO_versionTest.csv',
        'coles': '../data/raw/colesSAE_versionTest.csv',
        'oferta_test': '../data/raw/1_oferta_2019_test.csv',
        'fusiones': '../data/raw/fusionesAnexos_versionTest.csv',
        'codigos_nivel': '../data/raw/codigos_ens_grado_a_nivel_versionTest.csv',
        'matricula': '../data/raw/MATRICULA2018_versionTest.csv'
    }

    try:
        generar_oferta_2019(RUTAS, '../data/processed/1_oferta_2019.csv')
        generar_estudiantes_preinscribir(RUTAS, '../data/processed/2_estudiantes_a_preinscribir.csv')
        logging.info("Pipeline ejecutado con éxito en su totalidad.")
    except Exception as e:
        logging.critical(f"Fallo crítico en el pipeline: {e}")
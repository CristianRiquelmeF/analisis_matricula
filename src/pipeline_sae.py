import pandas as pd
import logging
from pathlib import Path

# =========================================================
# CONFIGURACIÓN GLOBAL


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

OUTPUT_SEP = "|"
FECHA_CORTE_EDAD = pd.Timestamp("2019-03-31")

# =========================================================
# FUNCIONES AUXILIARES (ETL ROBUSTO)


def cargar_csv_seguro(ruta, usecols=None, dtype=None):
    """Carga CSV con tolerancia a múltiples separadores y normaliza cabeceras."""
    separadores = ["|", ";", ","]
    for sep in separadores:
        try:
            df = pd.read_csv(
                ruta, sep=sep, encoding="latin-1", 
                usecols=usecols, dtype=dtype, low_memory=False
            )
            if usecols is None and len(df.columns) == 1:
                continue
            
            # Normalización estricta de nombres de columnas
            df.columns = df.columns.str.strip().str.lower()
            return df
        except Exception:
            continue
    raise ValueError(f"Fallo crítico: No fue posible leer {ruta} con los separadores conocidos.")

def auditar_dimensiones(df, nombre_paso):
    """Registra en el log el volumen de datos actual."""
    logging.info(f"[{nombre_paso}] -> {df.shape[0]} filas | {df.shape[1]} columnas")

def calcular_edad_exacta(fecha_nac, fecha_ref):
    """Cálculo determinista de edad en años cumplidos, resistente a años bisiestos."""
    edad = (
        fecha_ref.year - fecha_nac.dt.year -
        (
            (fecha_nac.dt.month > fecha_ref.month) | 
            ((fecha_nac.dt.month == fecha_ref.month) & (fecha_nac.dt.day > fecha_ref.day))
        ).astype(int)
    )
    return edad

# =========================================================
# PASO 1: CONSTRUCCIÓN DE LA OFERTA VÁLIDA


def generar_oferta_2019(rutas, salida):
    logging.info("Iniciando Paso 1: Generación de Oferta 2019")

    ficha = cargar_csv_seguro(rutas["ficha"])
    log = cargar_csv_seguro(rutas["log"], dtype={"rbd": "Int64", "id_log_cupo": "Int64"})
    anexo = cargar_csv_seguro(rutas["anexo"])
    admi = cargar_csv_seguro(rutas["admi"])
    coles = cargar_csv_seguro(rutas["coles"], dtype={"rbd": "Int64"})

    # 1. Filtro de Logs Válidos (Preservando múltiples cursos por RBD)
    log["fecha_ultima_actualizacion"] = pd.to_datetime(log["fecha_ultima_actualizacion"], errors="coerce")
    log_valido = log[log["cargado_vitrina"] == 1].copy()
    
    # Ordenamos y nos quedamos con la última actualización por cada declaración (id_log_cupo)
    log_valido = log_valido.sort_values(["rbd", "fecha_ultima_actualizacion", "id_log_cupo"])
    log_valido = log_valido.drop_duplicates(subset=["rbd", "id_log_cupo"], keep="last")
    
    oferta = ficha[ficha["id_log_cupo"].isin(log_valido["id_log_cupo"])].copy()

    # 2. Cruce con Anexos para determinar Sede
    anexo = anexo.rename(columns={"id_anexo": "id_sede_anexo"})
    oferta = oferta.merge(anexo[["id_sede_anexo", "n_correlativo"]], on="id_sede_anexo", how="left", validate="many_to_one")
    oferta["cod_sede"] = oferta["n_correlativo"].fillna(1).astype(int)

    # 3. Limpieza de Cupos
    oferta["total_cupos"] = pd.to_numeric(oferta["total_cupos"], errors="coerce")
    oferta = oferta[oferta["total_cupos"] > 0]

    # 4. Homologación de llaves para cruce con catálogo de cursos
    oferta["cod_genero"] = oferta["tipo_alumnado"]
    oferta["cod_jor"] = oferta["tipo_jornada"]
    oferta["cod_esp"] = pd.to_numeric(oferta["cod_esp"], errors="coerce").replace([99, 999, 9999, 99999], 0).fillna(0).astype(int)
    admi["cod_esp"] = pd.to_numeric(admi["cod_esp"], errors="coerce").fillna(0).astype(int)

    llaves_cruce = ["cod_gra", "cod_ens", "cod_esp", "cod_sede", "cod_genero", "cod_jor"]

    oferta = oferta.merge(
        admi[["cod_curso", "cod_nivel"] + llaves_cruce],
        on=llaves_cruce, how="inner", validate="many_to_one"
    )

# 5. Filtro final por colegios vigentes SAE
    oferta = oferta.merge(coles[["rbd"]], on="rbd", how="inner", validate="many_to_one")

    # 6. Ordenamos por RBD, luego por curso, y finalmente por el ID del log (cronológico)
    # Nos quedamos con la última declaración (keep='last') en caso de que hayan actualizado cupos.
    oferta = oferta.sort_values(by=["rbd", "cod_curso", "id_log_cupo"], ascending=[True, True, True])
    oferta = oferta.drop_duplicates(subset=["rbd", "cod_nivel", "cod_curso"], keep="last")

    # Formato final y exportación
    oferta_final = oferta[["rbd", "cod_nivel", "cod_curso", "total_cupos"]].copy()
    oferta_final = oferta_final.astype({"rbd": int, "cod_nivel": int, "total_cupos": int})
    
    # Comprobación de integridad 
    if oferta_final.duplicated(subset=["rbd", "cod_nivel", "cod_curso"]).sum() > 0:
        raise ValueError("Error de integridad: Cursos duplicados en la oferta final.")

    oferta_final.to_csv(salida, sep=OUTPUT_SEP, index=False)
    auditar_dimensiones(oferta_final, "Oferta 2019 Final")

# =========================================================
# PASO 2: CÁLCULO DE CONTINUIDAD DE MATRÍCULA


def generar_estudiantes_preinscribir(rutas, salida):
    logging.info("Iniciando Paso 2: Cálculo de Matrícula Asegurada")

    oferta_test = cargar_csv_seguro(rutas["oferta_test"])
    fusiones = cargar_csv_seguro(rutas["fusiones"])
    codigos = cargar_csv_seguro(rutas["codigos_nivel"])
    
    # Optimizamos RAM cargando solo lo necesario
    cols_mat = ['sal_run', 'rbd', 'fecha_incorporacion', 'fecha_retiro', 'cod_ens', 'cod_gra', 'sal_fec_nac']
    matricula = cargar_csv_seguro(rutas["matricula"], usecols=cols_mat)
    auditar_dimensiones(matricula, "Ingesta Matrícula Bruta")

    # 1. Transformación de fechas
    matricula["fecha_incorporacion"] = pd.to_datetime(matricula["fecha_incorporacion"], format="%Y-%m-%d", errors="coerce")
    matricula["fecha_retiro"] = pd.to_datetime(matricula["fecha_retiro"], format="%Y-%m-%d", errors="coerce")
    matricula["sal_fec_nac"] = pd.to_datetime(matricula["sal_fec_nac"], format="%Y-%m-%d", errors="coerce")

    # 2. Filtros Normativos Base (Aplicados tempranamente para liberar RAM)
    matricula = matricula[matricula["fecha_retiro"].isna()]
    matricula = matricula[matricula["cod_ens"].isin([10, 110, 310, 410, 510, 610, 710, 810, 910])]
    
    matricula["edad_2019"] = calcular_edad_exacta(matricula["sal_fec_nac"], FECHA_CORTE_EDAD)
    matricula = matricula[matricula["edad_2019"] >= 4]

    # 3. Asignación de Nivel Actual y Filtro de Prekinder
    matricula = matricula.merge(codigos[["cod_ens", "cod_gra", "cod_nivel"]], on=["cod_ens", "cod_gra"], how="inner", validate="many_to_one")
    matricula = matricula[matricula["cod_nivel"] >= -1]
    matricula["cod_nivel_2019"] = matricula["cod_nivel"] + 1

    # 4. Tratamiento de Fusiones (Proyección del RBD)
    fusiones = fusiones.rename(columns={"rbd_principal": "rbd_2019"})
    matricula = matricula.merge(fusiones[["rbd", "rbd_2019"]], on="rbd", how="left")
    matricula["rbd_2019"] = matricula["rbd_2019"].fillna(matricula["rbd"]).astype(int)

    # 5. Cruce Estructural con Oferta 2019
    oferta_pares = oferta_test[["rbd", "cod_nivel"]].drop_duplicates().rename(columns={"rbd": "rbd_2019", "cod_nivel": "cod_nivel_2019"})
    matricula = matricula.merge(oferta_pares, on=["rbd_2019", "cod_nivel_2019"], how="inner")
    
    auditar_dimensiones(matricula, "Matrícula Post-Filtros y Continuidad")

    # 6. Resolución de Conflictos (Duplicidad)
    # Ordenamos: RUN -> Fecha (Más reciente) -> RBD (Priorizar mayor como desempate determinista)
    matricula = matricula.sort_values(["sal_run", "fecha_incorporacion", "rbd_2019"], ascending=[True, False, False])
    matricula = matricula.drop_duplicates(subset=["sal_run"], keep="first")

    # Formato final y exportación
    final = matricula[["sal_run", "rbd_2019", "cod_nivel_2019"]].copy()
    final = final.astype({"rbd_2019": int, "cod_nivel_2019": int})

    # Auditoría Final Restrictiva
    if final["sal_run"].duplicated().sum() > 0:
        raise ValueError("Error de integridad: RUNs duplicados en la nómina de preinscripción.")

    final.to_csv(salida, sep=OUTPUT_SEP, index=False)
    auditar_dimensiones(final, "Nómina Final Preinscripción")

# =========================================================
# EJECUCIÓN DEL PIPELINE


if __name__ == "__main__":
    Path("../data/processed").mkdir(parents=True, exist_ok=True)

    RUTAS = {
        "ficha": "../data/raw/SIGE_SAE_LOG_FICHA_CUPO_versionTest.csv",
        "log": "../data/raw/SIGE_SAE_LOG_CUPO_versionTest.csv",
        "anexo": "../data/raw/SIGE_SAE_LOG_ANEXO_SEDE_versionTest.csv",
        "admi": "../data/raw/ADMI_CURSO_versionTest.csv",
        "coles": "../data/raw/colesSAE_versionTest.csv",
        "oferta_test": "../data/raw/1_oferta_2019_test.csv",
        "fusiones": "../data/raw/fusionesAnexos_versionTest.csv",
        "codigos_nivel": "../data/raw/codigos_ens_grado_a_nivel_versionTest.csv",
        "matricula": "../data/raw/MATRICULA2018_versionTest.csv"
    }

    try:
        generar_oferta_2019(RUTAS, "../data/processed/1_oferta_2019.csv")
        generar_estudiantes_preinscribir(RUTAS, "../data/processed/2_estudiantes_a_preinscribir.csv")
        logging.info("✅ PIPELINE SAE COMPLETADO EXITOSAMENTE")
    except Exception as e:
        logging.exception(f"❌ ERROR CRÍTICO EN EL PIPELINE: {e}")
        raise
    
    


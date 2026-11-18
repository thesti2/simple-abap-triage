*&---------------------------------------------------------------------*
*& Report ztriage_job_monitor
*&---------------------------------------------------------------------*
*&
*&---------------------------------------------------------------------*
REPORT ztriage_job_monitor.
*&---------------------------------------------------------------------*
*& ABAP Triage MVP - send cancelled background jobs to xMatters.
*& Schedule in SM36 as job ZTRIAGE_JOB_MONITOR every 5 minutes.
*& Logic: ZCL_TRIAGE_JOB_MONITOR. Already sent jobs are skipped.
*& P_TEST = X: only list what would be sent (no HTTP call).
*&---------------------------------------------------------------------*
TABLES tbtco.

SELECT-OPTIONS s_job FOR tbtco-jobname.
PARAMETERS:
  p_dest TYPE rfcdest DEFAULT 'ZTRIAGE_XMATTERS' OBLIGATORY,
  p_days TYPE i DEFAULT 1,
  p_test AS CHECKBOX DEFAULT ' ',
  p_json AS CHECKBOX DEFAULT ' '.

START-OF-SELECTION.
  DATA gt_alert TYPE zcl_triage_job_monitor=>tt_alert.
  DATA(go_monitor) = NEW zcl_triage_job_monitor( iv_dest = p_dest iv_days = p_days ).
  DATA(gt_result) = go_monitor->run( EXPORTING it_jobname = s_job[]
                                               iv_test    = p_test
                                     IMPORTING et_alert   = gt_alert ).

  WRITE: / |Cancelled jobs found (not yet sent): { lines( gt_alert ) }|.
  IF p_test = abap_true.
    WRITE: / 'TEST MODE - nothing sent.'.
  ENDIF.
  ULINE.

  LOOP AT gt_alert INTO DATA(gs_alert).
    WRITE: / gs_alert-alert_id,
           / |  Program { gs_alert-program } variant { gs_alert-variant } user { gs_alert-job_user }|,
           / |  { gs_alert-error_type }: { gs_alert-runtime_error } { gs_alert-error_message }|.
    READ TABLE gt_result INTO DATA(gs_result) WITH KEY alert_id = gs_alert-alert_id.
    IF sy-subrc = 0.
      WRITE: / |  Sent: { COND #( WHEN gs_result-ok = abap_true THEN 'OK' ELSE 'FAILED' ) } - { gs_result-message }|.
      IF gs_result-ok = abap_false.
        MESSAGE |xMatters send failed for { gs_alert-alert_id }: { gs_result-message }| TYPE 'S' DISPLAY LIKE 'W'.
      ENDIF.
    ENDIF.
    IF p_json = abap_true.
      WRITE: / go_monitor->to_json( gs_alert ).
    ENDIF.
    ULINE.
  ENDLOOP.

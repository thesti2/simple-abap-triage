"! ABAP Triage MVP - collect cancelled background jobs (TBTCO status A),
"! enrich them with step program/variant, job log and short dump, and send
"! them as flat JSON to xMatters via SM59 destination (type G).
"! Already sent jobs are remembered in cluster table INDX (ID ZTRIAGE_SENT).
CLASS zcl_triage_job_monitor DEFINITION
  PUBLIC
  FINAL
  CREATE PUBLIC .

  PUBLIC SECTION.
    TYPES:
      BEGIN OF ty_alert,
        alert_id      TYPE string,
        source        TYPE string,
        system        TYPE string,
        client        TYPE string,
        job_name      TYPE string,
        job_count     TYPE string,
        job_status    TYPE string,
        job_user      TYPE string,
        scheduled_by  TYPE string,
        start_time    TYPE string,
        end_time      TYPE string,
        timezone      TYPE string,
        program       TYPE string,
        variant       TYPE string,
        error_type    TYPE string,  " DUMP | MESSAGE | UNKNOWN
        runtime_error TYPE string,
        error_program TYPE string,
        error_include TYPE string,
        error_line    TYPE string,
        error_message TYPE string,
        joblog_text   TYPE string,
        joblog        TYPE string_table,
      END OF ty_alert,
      tt_alert TYPE STANDARD TABLE OF ty_alert WITH EMPTY KEY,
      tt_jobname_range TYPE RANGE OF btcjob,
      BEGIN OF ty_send_result,
        alert_id    TYPE string,
        ok          TYPE abap_bool,
        http_status TYPE i,
        message     TYPE string,
      END OF ty_send_result,
      tt_send_result TYPE STANDARD TABLE OF ty_send_result WITH EMPTY KEY.

    CONSTANTS c_default_dest TYPE rfcdest VALUE 'ZTRIAGE_XMATTERS'.

    METHODS constructor
      IMPORTING iv_dest TYPE rfcdest DEFAULT c_default_dest
                iv_days TYPE i DEFAULT 1.

    "! Cancelled jobs not yet sent (or all, if iv_ignore_sent = X).
    METHODS collect
      IMPORTING it_jobname      TYPE tt_jobname_range OPTIONAL
                iv_ignore_sent  TYPE abap_bool DEFAULT abap_false
      RETURNING VALUE(rt_alert) TYPE tt_alert.

    METHODS to_json
      IMPORTING is_alert       TYPE ty_alert
      RETURNING VALUE(rv_json) TYPE string.

    METHODS send
      IMPORTING is_alert         TYPE ty_alert
      RETURNING VALUE(rs_result) TYPE ty_send_result.

    "! Collect + send + remember successful sends. iv_test = X: collect only.
    METHODS run
      IMPORTING it_jobname       TYPE tt_jobname_range OPTIONAL
                iv_test          TYPE abap_bool DEFAULT abap_false
      EXPORTING et_alert         TYPE tt_alert
      RETURNING VALUE(rt_result) TYPE tt_send_result.

  PROTECTED SECTION.
  PRIVATE SECTION.
    TYPES:
      BEGIN OF ty_sent,
        jobname  TYPE btcjob,
        jobcount TYPE btcjobcnt,
        sent_on  TYPE d,
      END OF ty_sent,
      tt_sent TYPE HASHED TABLE OF ty_sent WITH UNIQUE KEY jobname jobcount.

    CONSTANTS c_indx_id TYPE indx-srtfd VALUE 'ZTRIAGE_SENT'.

    DATA mv_dest TYPE rfcdest.
    DATA mv_days TYPE i.
    DATA mo_runner TYPE REF TO zcl_triage_runner.

    METHODS load_sent RETURNING VALUE(rt_sent) TYPE tt_sent.
    METHODS save_sent IMPORTING it_sent TYPE tt_sent.
    METHODS iso
      IMPORTING iv_date       TYPE d
                iv_time       TYPE t
      RETURNING VALUE(rv_iso) TYPE string.
ENDCLASS.



CLASS zcl_triage_job_monitor IMPLEMENTATION.

  METHOD constructor.
    mv_dest   = iv_dest.
    mv_days   = iv_days.
    mo_runner = NEW #( ).
  ENDMETHOD.


  METHOD collect.
    DATA lt_log TYPE STANDARD TABLE OF tbtc5.
    DATA lv_tz  TYPE ttzcu-tzonesys.

    DATA(lv_from) = CONV d( sy-datum - mv_days ).
    DATA(lt_sent) = COND tt_sent( WHEN iv_ignore_sent = abap_false THEN load_sent( ) ).

    SELECT SINGLE tzonesys FROM ttzcu INTO @lv_tz.

    SELECT jobname, jobcount, sdluname, strtdate, strttime, enddate, endtime
      FROM tbtco
      WHERE status   = 'A'
        AND enddate >= @lv_from
        AND jobname IN @it_jobname
        AND jobname NOT LIKE 'ZTRIAGE_RUN_%'      " reproduce runs of the fix agent
        AND jobname <> 'ZTRIAGE_JOB_MONITOR'
      ORDER BY enddate, endtime
      INTO TABLE @DATA(lt_job).

    LOOP AT lt_job INTO DATA(ls_job).
      IF line_exists( lt_sent[ jobname = ls_job-jobname jobcount = ls_job-jobcount ] ).
        CONTINUE.
      ENDIF.

      SELECT stepcount, progname, variant, authcknam FROM tbtcp
        WHERE jobname = @ls_job-jobname AND jobcount = @ls_job-jobcount
        ORDER BY stepcount
        INTO TABLE @DATA(lt_step).
      DATA(ls_step) = VALUE #( lt_step[ 1 ] OPTIONAL ).
      DATA(lv_user) = COND syuname( WHEN ls_step-authcknam IS NOT INITIAL THEN ls_step-authcknam
                                    ELSE ls_job-sdluname ).

      DATA(ls_alert) = VALUE ty_alert(
        alert_id     = |{ sy-sysid }-{ sy-mandt }-{ ls_job-jobname }-{ ls_job-jobcount }|
        source       = 'SAP'
        system       = sy-sysid
        client       = sy-mandt
        job_name     = ls_job-jobname
        job_count    = ls_job-jobcount
        job_status   = 'CANCELLED'
        job_user     = lv_user
        scheduled_by = ls_job-sdluname
        start_time   = iso( iv_date = ls_job-strtdate iv_time = ls_job-strttime )
        end_time     = iso( iv_date = ls_job-enddate iv_time = ls_job-endtime )
        timezone     = lv_tz
        program      = ls_step-progname
        variant      = ls_step-variant ).

      " short dump in the job's runtime window
      IF ls_job-strtdate IS NOT INITIAL.
        DATA(ls_dump) = mo_runner->find_dump( iv_user      = lv_user
                                              iv_from_date = ls_job-strtdate
                                              iv_from_time = ls_job-strttime
                                              iv_to_date   = ls_job-enddate
                                              iv_to_time   = ls_job-endtime ).
        ls_alert-runtime_error = ls_dump-runtime_error.
        ls_alert-error_program = ls_dump-program.
        ls_alert-error_include = ls_dump-include.
        ls_alert-error_line    = ls_dump-line.
        " if the job had several steps, report the step that dumped
        IF ls_dump-program IS NOT INITIAL AND line_exists( lt_step[ progname = ls_dump-program ] ).
          ls_alert-program = lt_step[ progname = ls_dump-program ]-progname.
          ls_alert-variant = lt_step[ progname = ls_dump-program ]-variant.
        ENDIF.
      ENDIF.

      " job log + first meaningful error message
      CLEAR lt_log.
      CALL FUNCTION 'BP_JOBLOG_READ'
        EXPORTING  client    = sy-mandt
                   jobcount  = ls_job-jobcount
                   jobname   = ls_job-jobname
        TABLES     joblogtbl = lt_log
        EXCEPTIONS OTHERS    = 1.
      IF sy-subrc = 0.
        LOOP AT lt_log INTO DATA(ls_log).
          APPEND |{ ls_log-enterdate DATE = ISO } { ls_log-entertime TIME = ISO } { ls_log-msgtype } { ls_log-msgid }{ ls_log-msgno } { ls_log-text }| TO ls_alert-joblog.
          " skip generic BP messages: 516 start, 517 finished, 518 canceled, 550 step, 564 canceled after exception
          IF ls_alert-error_message IS INITIAL AND ( ls_log-msgtype = 'E' OR ls_log-msgtype = 'A' OR ls_log-msgtype = 'X' )
             AND NOT ( ls_log-msgid = '00' AND ( ls_log-msgno = '518' OR ls_log-msgno = '564' ) ).
            ls_alert-error_message = ls_log-text.
          ENDIF.
        ENDLOOP.
      ENDIF.
      ls_alert-joblog_text = concat_lines_of( table = ls_alert-joblog sep = |\n| ).

      ls_alert-error_type = COND #( WHEN ls_alert-runtime_error IS NOT INITIAL THEN 'DUMP'
                                    WHEN ls_alert-error_message IS NOT INITIAL THEN 'MESSAGE'
                                    ELSE 'UNKNOWN' ).
      APPEND ls_alert TO rt_alert.
      CLEAR ls_dump.
    ENDLOOP.
  ENDMETHOD.


  METHOD to_json.
    rv_json = /ui2/cl_json=>serialize( data        = is_alert
                                       compress    = abap_false
                                       pretty_name = /ui2/cl_json=>pretty_mode-low_case ).
  ENDMETHOD.


  METHOD send.
    DATA lo_client TYPE REF TO if_http_client.

    rs_result-alert_id = is_alert-alert_id.

    cl_http_client=>create_by_destination(
      EXPORTING  destination = mv_dest
      IMPORTING  client      = lo_client
      EXCEPTIONS OTHERS      = 1 ).
    IF sy-subrc <> 0.
      rs_result-message = |Cannot create HTTP client for destination { mv_dest }|.
      RETURN.
    ENDIF.

    lo_client->request->set_method( if_http_request=>co_request_method_post ).
    lo_client->request->set_header_field( name = 'Content-Type' value = 'application/json' ).
    lo_client->request->set_cdata( to_json( is_alert ) ).

    lo_client->send( EXCEPTIONS OTHERS = 1 ).
    IF sy-subrc = 0.
      lo_client->receive( EXCEPTIONS OTHERS = 1 ).
    ENDIF.
    IF sy-subrc <> 0.
      lo_client->get_last_error( IMPORTING message = rs_result-message ).
      lo_client->close( ).
      RETURN.
    ENDIF.

    lo_client->response->get_status( IMPORTING code = rs_result-http_status ).
    DATA(lv_body) = lo_client->response->get_cdata( ).
    rs_result-ok      = xsdbool( rs_result-http_status BETWEEN 200 AND 299 ).
    rs_result-message = |HTTP { rs_result-http_status } { substring( val = lv_body len = nmin( val1 = 200 val2 = strlen( lv_body ) ) ) }|.
    lo_client->close( ).
  ENDMETHOD.


  METHOD run.
    et_alert = collect( it_jobname = it_jobname ).
    IF iv_test = abap_true OR et_alert IS INITIAL.
      RETURN.
    ENDIF.

    DATA(lt_sent) = load_sent( ).
    LOOP AT et_alert INTO DATA(ls_alert).
      DATA(ls_result) = send( ls_alert ).
      APPEND ls_result TO rt_result.
      IF ls_result-ok = abap_true.
        INSERT VALUE #( jobname = ls_alert-job_name jobcount = ls_alert-job_count sent_on = sy-datum )
          INTO TABLE lt_sent.
      ENDIF.
    ENDLOOP.
    save_sent( lt_sent ).
  ENDMETHOD.


  METHOD load_sent.
    IMPORT sent = rt_sent FROM DATABASE indx(zt) ID c_indx_id.
    IF sy-subrc <> 0.
      CLEAR rt_sent.
    ENDIF.
  ENDMETHOD.


  METHOD save_sent.
    DATA(lt_sent) = it_sent.
    " keep 30 days of history
    DATA(lv_limit) = CONV d( sy-datum - 30 ).
    DELETE lt_sent WHERE sent_on < lv_limit.
    EXPORT sent = lt_sent TO DATABASE indx(zt) ID c_indx_id.
    COMMIT WORK.
  ENDMETHOD.


  METHOD iso.
    IF iv_date IS INITIAL.
      RETURN.
    ENDIF.
    rv_iso = |{ iv_date DATE = ISO }T{ iv_time TIME = ISO }|.
  ENDMETHOD.

ENDCLASS.

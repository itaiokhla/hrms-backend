from fastapi import APIRouter, Depends, HTTPException, status, File, UploadFile, Query
from sqlalchemy.orm import Session
from app.database.db import get_db
from app.models.employee import Employee, FaceEncoding
from app.models.attendance import Attendance
from app.modules.face_recognition.face_encoder import FaceEncoder
from app.modules.face_recognition.face_matcher import FaceMatcher
from app.modules.face_recognition.face_processor import FaceProcessor
from app.config import settings
from datetime import date, datetime, timedelta
import logging
import cv2
import numpy as np
from pathlib import Path
import io

router = APIRouter(prefix="/api/v1", tags=["face-recognition"])
logger = logging.getLogger(__name__)

face_matcher = FaceMatcher(threshold=settings.face_recognition_threshold)
face_processor = FaceProcessor(confidence_threshold=settings.face_detection_confidence)


@router.post("/face-recognition/register")
async def register_face_encoding(
    employee_id: int = Query(...),
    file: UploadFile = File(...),
    is_primary: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Register face encoding for an employee"""
    try:
        employee = db.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="Employee not found")

        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            raise HTTPException(status_code=400, detail="Invalid image file")

        result = FaceEncoder.encode_face(img)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        if is_primary:
            existing_primary = db.query(FaceEncoding).filter(
                FaceEncoding.employee_id == employee_id,
                FaceEncoding.is_primary == True,
            ).first()
            if existing_primary:
                existing_primary.is_primary = False

        upload_dir = Path("./uploads/faces")
        upload_dir.mkdir(parents=True, exist_ok=True)

        file_path = upload_dir / f"employee_{employee_id}_{len(employee.face_encodings) + 1}.jpg"
        with open(file_path, "wb") as f:
            f.write(contents)

        face_encoding = FaceEncoding(
            employee_id=employee_id,
            encoding=result["encoding"],
            image_url=str(file_path),
            is_primary=is_primary,
        )

        db.add(face_encoding)
        db.commit()
        db.refresh(face_encoding)

        return {
            "id": face_encoding.id,
            "employee_id": employee_id,
            "is_primary": is_primary,
            "image_url": str(file_path),
            "message": "Face encoding registered successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error registering face: {e}")
        raise HTTPException(status_code=500, detail="Failed to register face encoding")


@router.post("/face-recognition/identify")
async def identify_face(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Identify employee from face image"""
    try:
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            raise HTTPException(status_code=400, detail="Invalid image file")

        result = FaceEncoder.encode_face(img)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        test_encoding_str = result["encoding"]

        from app.modules.face_recognition.face_encoder import FaceEncoder as FE
        test_encoding = FE._decode_from_string(test_encoding_str)

        primary_encodings = db.query(FaceEncoding).filter(
            FaceEncoding.is_primary == True
        ).all()

        if not primary_encodings:
            raise HTTPException(status_code=404, detail="No registered faces found")

        known_encodings = [(e.employee_id, e.encoding) for e in primary_encodings]

        match = face_matcher.find_best_match(test_encoding, known_encodings)

        if not match:
            return {
                "identified": False,
                "confidence": 0.0,
                "message": "No matching face found",
            }

        employee_id, confidence = match
        employee = db.query(Employee).filter(Employee.id == employee_id).first()

        return {
            "identified": True,
            "employee_id": employee_id,
            "employee_name": f"{employee.first_name} {employee.last_name}",
            "confidence": float(confidence),
            "email": employee.email,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error identifying face: {e}")
        raise HTTPException(status_code=500, detail="Failed to identify face")


@router.post("/face-recognition/check-in-face")
async def check_in_with_face(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Check in employee using face recognition"""
    try:
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            raise HTTPException(status_code=400, detail="Invalid image file")

        result = FaceEncoder.encode_face(img)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        test_encoding_str = result["encoding"]

        from app.modules.face_recognition.face_encoder import FaceEncoder as FE
        test_encoding = FE._decode_from_string(test_encoding_str)

        primary_encodings = db.query(FaceEncoding).filter(
            FaceEncoding.is_primary == True
        ).all()

        if not primary_encodings:
            raise HTTPException(status_code=404, detail="No registered faces found")

        known_encodings = [(e.employee_id, e.encoding) for e in primary_encodings]
        match = face_matcher.find_best_match(test_encoding, known_encodings)

        if not match:
            return {
                "success": False,
                "message": "Face not recognized",
            }

        employee_id, confidence = match

        if confidence < settings.face_recognition_threshold:
            return {
                "success": False,
                "message": "Face confidence too low",
                "confidence": float(confidence),
            }

        today = date.today()
        existing_checkin = db.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.date == today,
            Attendance.check_in_time != None,
        ).first()

        if existing_checkin and not existing_checkin.check_out_time:
            return {
                "success": False,
                "message": "Already checked in today",
                "employee_id": employee_id,
            }

        from app.services.attendance_service import AttendanceService
        attendance = AttendanceService.check_in(db, employee_id, float(confidence), is_manual=False)

        employee = db.query(Employee).filter(Employee.id == employee_id).first()

        return {
            "success": True,
            "message": "Check-in successful",
            "employee_id": employee_id,
            "employee_name": f"{employee.first_name} {employee.last_name}",
            "confidence": float(confidence),
            "check_in_time": attendance.check_in_time.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in face recognition check-in: {e}")
        raise HTTPException(status_code=500, detail="Failed to process face check-in")


@router.post("/face-recognition/check-out-face")
async def check_out_with_face(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Check out employee using face recognition"""
    try:
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            raise HTTPException(status_code=400, detail="Invalid image file")

        result = FaceEncoder.encode_face(img)

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        from app.modules.face_recognition.face_encoder import FaceEncoder as FE

        test_encoding = FE._decode_from_string(result["encoding"])

        primary_encodings = db.query(FaceEncoding).filter(
            FaceEncoding.is_primary == True
        ).all()

        if not primary_encodings:
            raise HTTPException(
                status_code=404,
                detail="No registered faces found"
            )

        known_encodings = [
            (e.employee_id, e.encoding)
            for e in primary_encodings
        ]

        match = face_matcher.find_best_match(
            test_encoding,
            known_encodings
        )

        if not match:
            return {
                "success": False,
                "message": "Face not recognized"
            }

        employee_id, confidence = match

        from app.services.attendance_service import AttendanceService

        attendance = AttendanceService.check_out(
            db,
            employee_id
        )

        employee = db.query(Employee).filter(
            Employee.id == employee_id
        ).first()

        working_hours = (
            attendance.check_out_time -
            attendance.check_in_time
        )

        return {
            "success": True,
            "employee_id": employee_id,
            "employee_name": f"{employee.first_name} {employee.last_name}",
            "check_out_time": attendance.check_out_time.isoformat(),
            "working_hours": str(working_hours),
            "message": "Check-out successful"
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.error(e)

        raise HTTPException(
            status_code=500,
            detail="Failed to process face check-out"
        )        
@router.post("/face-recognition/scan")
async def smart_face_scan(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Smart Face Attendance
    - First Scan  -> Check In
    - Second Scan -> Check Out (after 1 minute)
    - Third Scan  -> Attendance Completed
    """

    try:
        contents = await file.read()

        nparr = np.frombuffer(contents, np.uint8)

        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid image"
            )

        result = FaceEncoder.encode_face(img)

        if "error" in result:
            raise HTTPException(
                status_code=400,
                detail=result["error"]
            )

        from app.modules.face_recognition.face_encoder import FaceEncoder as FE

        test_encoding = FE._decode_from_string(
            result["encoding"]
        )

        primary_encodings = (
            db.query(FaceEncoding)
            .filter(FaceEncoding.is_primary == True)
            .all()
        )

        if not primary_encodings:
            raise HTTPException(
                status_code=404,
                detail="No registered faces found"
            )

        known_encodings = [
            (e.employee_id, e.encoding)
            for e in primary_encodings
        ]

        match = face_matcher.find_best_match(
            test_encoding,
            known_encodings
        )

        if not match:
            return {
                "success": False,
                "message": "Face not recognized"
            }

        employee_id, confidence = match

        employee = (
            db.query(Employee)
            .filter(Employee.id == employee_id)
            .first()
        )

        today = date.today()

        attendance = (
            db.query(Attendance)
            .filter(
                Attendance.employee_id == employee_id,
                Attendance.date == today
            )
            .first()
        )

        from app.services.attendance_service import AttendanceService

        # ------------------------
        # CHECK IN
        # ------------------------

        if attendance is None:

            attendance = AttendanceService.check_in(
                db=db,
                employee_id=employee_id,
                face_match_score=float(confidence),
                is_manual=False,
            )

            return {
                "success": True,
                "action": "check_in",
                "employee_id": employee_id,
                "employee_name": f"{employee.first_name} {employee.last_name}",
                "time": attendance.check_in_time.isoformat(),
                "message": "Check-In Successful",
            }

        # ------------------------
        # CHECK OUT
        # ------------------------

        if attendance.check_out_time is None:

            elapsed = datetime.now() - attendance.check_in_time

            if elapsed < timedelta(minutes=1):

                remaining = 60 - int(elapsed.total_seconds())

                return {
                    "success": False,
                    "action": "wait",
                    "employee_id": employee_id,
                    "employee_name": f"{employee.first_name} {employee.last_name}",
                    "message": f"Please wait {remaining} seconds before check out.",
                }

            attendance = AttendanceService.check_out(
                db,
                employee_id
            )

            working_hours = (
                attendance.check_out_time -
                attendance.check_in_time
            )

            return {
                "success": True,
                "action": "check_out",
                "employee_id": employee_id,
                "employee_name": f"{employee.first_name} {employee.last_name}",
                "time": attendance.check_out_time.isoformat(),
                "working_hours": str(working_hours),
                "message": "Check-Out Successful",
            }

        # ------------------------
        # COMPLETED
        # ------------------------

        return {
            "success":  True,
            "action": "completed",
            "employee_id": employee_id,
            "employee_name": f"{employee.first_name} {employee.last_name}",
            "message": "Attendance Already Completed",
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Smart Scan Error: {e}")

        raise HTTPException(
            status_code=500,
            detail="Smart Scan Failed"
        )
        

    

@router.get("/face-recognition/employee/{employee_id}/encodings")
async def get_employee_encodings(
    employee_id: int,
    db: Session = Depends(get_db),
):
    """Get face encodings count for employee"""
    encodings = db.query(FaceEncoding).filter(
        FaceEncoding.employee_id == employee_id
    ).all()

    return {
        "employee_id": employee_id,
        "total_encodings": len(encodings),
        "primary_encoding": any(e.is_primary for e in encodings),
        "encodings": [
            {
                "id": e.id,
                "is_primary": e.is_primary,
                "created_at": e.created_at.isoformat(),
            }
            for e in encodings
        ],
    }

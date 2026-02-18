from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app
from ..extensions import db
from ..models import Expense, User
from ..utils import runMonthlyEvaluation, CATS, detect_anomalies, categorize_with_ai, generate_spending_insights
from sqlalchemy import func
from werkzeug.utils import secure_filename
import os
import pdfplumber
import re
import hashlib
from datetime import datetime, timedelta

bp = Blueprint('transactions', __name__)

@bp.route('/add', methods=['POST'])
def add_expense():
    if 'user_id' not in session: return redirect(url_for('auth.login'))
    title = request.form.get('title')
    amount = float(request.form.get('amount'))
    category = request.form.get('category')
    include_in_total = 'include_total' in request.form
    
    new_exp = Expense(user_id=session['user_id'], title=title, amount=abs(amount), 
                      category=category, type="Paid" if amount > 0 else "Received",
                      include_in_total=include_in_total)
    db.session.add(new_exp); db.session.commit()
    runMonthlyEvaluation(session['user_id'])
    
    # AI PRODUCTION MODULES (Production Real-time Async)
    detect_anomalies(session['user_id'], new_exp.id)
    categorize_with_ai(new_exp.id)
    now = datetime.utcnow()
    generate_spending_insights(session['user_id'], now.year, now.month)

    flash('Expense Added!', 'success')
    return redirect(url_for('transactions.manual'))

@bp.route('/delete/<id>')
def delete_expense(id):
    if 'user_id' not in session: return redirect(url_for('auth.login'))
    exp = Expense.query.get_or_404(id)
    # Ensure ID comparison works (UUID str vs UUID obj usually handled by SA, 
    # but strictly casting user_id to string for safety if exp.user_id is UUID obj)
    if str(exp.user_id) == str(session['user_id']):
        db.session.delete(exp); db.session.commit()
        runMonthlyEvaluation(session['user_id'])
        flash('Deleted successfully.', 'info')
    return redirect(request.referrer or '/')

import google.generativeai as genai
import json

@bp.route('/parser', methods=['GET', 'POST'])
def parser():
    if 'user_id' not in session: return redirect(url_for('auth.login'))
    u_id = session['user_id']
    if request.method == 'POST':
        file = request.files.get('statement')
        if file and file.filename.endswith('.pdf'):
            filename = secure_filename(file.filename)
            fpath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            file.save(fpath)
            
            try:
                # 1. Extract Text
                with pdfplumber.open(fpath) as pdf:
                    text = ""
                    for page in pdf.pages: text += page.extract_text() or ""
                
                # 2. Configure GenAI
                api_key = os.getenv('GOOGLE_API_KEY')
                if not api_key:
                    flash('Server Error: GOOGLE_API_KEY missing.', 'danger')
                    return redirect(url_for('transactions.parser'))

                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-flash-latest')

                # 3. Prompt Engineering
                prompt = f"""
                You are a financial data extraction AI. Analyze the following bank statement text and extract all transactions.
                
                Rules:
                1. Return ONLY raw JSON array. No markdown formatting.
                2. Structure: [{{"date": "YYYY-MM-DD", "description": "Merchant/Details", "amount": 10.50, "category": "CategoryName", "type": "Paid" or "Received"}}]
                3. "Paid" = Debits/Withdrawals, "Received" = Credits/Deposits.
                4. Ignore non-transaction lines (headers, balances).
                5. Guess the category (Food, Travel, Bills, Shopping, Salary, Investment, Others).
                
                Text Data:
                {text[:30000]} 
                """
                # Limit text to avoid token limits if PDF is huge, though 1.5 Flash handles 1M tokens.

                response = model.generate_content(prompt)
                
                # 4. Clean & Parse JSON
                content = response.text.strip()
                if content.startswith('```json'): content = content[7:-3]
                transactions = json.loads(content)
                
                count = 0
                for t in transactions:
                    # Validate
                    if not t.get('amount'): continue
                    
                    amt = float(t['amount'])
                    raw_date = t.get('date', datetime.utcnow().strftime('%Y-%m-%d'))
                    desc = t.get('description', 'Unknown')
                    tran_type = t.get('type', 'Paid')

                    # DUPLICATE PROTECTION: Unique Hash for Ledger Consistency
                    hash_str = f"{u_id}-{raw_date}-{desc}-{abs(amt)}-{tran_type}"
                    t_hash = hashlib.sha256(hash_str.encode()).hexdigest()

                    # Check if exists
                    if Expense.query.filter_by(transaction_hash=t_hash).first():
                        continue

                    new_e = Expense(
                        user_id=u_id, 
                        title=desc[:100], 
                        amount=abs(amt), 
                        category=t.get('category', 'Others'), 
                        type=tran_type,
                        expense_date=datetime.strptime(raw_date, '%Y-%m-%d') if raw_date else datetime.utcnow(),
                        is_parsed=True,
                        statement_tag=filename,
                        transaction_hash=t_hash
                    )
                    db.session.add(new_e)
                    count += 1
                    
                db.session.commit()
                runMonthlyEvaluation(u_id)
                
                # AI PRODUCTION MODULES: Batch Insight
                now = datetime.utcnow()
                generate_spending_insights(u_id, now.year, now.month)
                # Note: For batch uploads, we don't trigger anomaly detection on every line to stay within rate limits.
                # Real fintech apps would queue these.
                
                flash(f'Success! AI extracted {count} transactions.', 'success')

            except Exception as e:
                flash(f'AI Parsing Failed: {str(e)}', 'danger')
            return redirect(url_for('transactions.parser'))
            
    expenses = Expense.query.filter_by(user_id=u_id, is_parsed=True).order_by(Expense.expense_date.desc()).all()
    p_paid = db.session.query(func.sum(Expense.amount)).filter_by(user_id=u_id, is_parsed=True, type='Paid').scalar() or 0
    p_received = db.session.query(func.sum(Expense.amount)).filter_by(user_id=u_id, is_parsed=True, type='Received').scalar() or 0
    return render_template('parser.html', expenses=expenses, p_paid=p_paid, p_received=p_received)

@bp.route('/receipts', methods=['GET', 'POST'])
def receipts():
    if 'user_id' not in session: return redirect(url_for('auth.login'))
    u_id = session['user_id']
    if request.method == 'POST':
        title = request.form.get('title')
        amount = float(request.form.get('amount') or 0)
        category = request.form.get('category', 'Others')
        file = request.files.get('file')
        
        filename = None
        if file:
            filename = secure_filename(file.filename)
            fpath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
            file.save(fpath)
            
        new_e = Expense(user_id=u_id, title=title, amount=amount, 
                        category=category, attachment_url=filename)
        db.session.add(new_e); db.session.commit()
        runMonthlyEvaluation(u_id)
        flash('Receipt saved to Vault!', 'success')
        return redirect(url_for('transactions.receipts'))
            
    images = Expense.query.filter(Expense.user_id == u_id, Expense.attachment_url != None).all()
    # Map 'attachment_url' to 'attachment' for template compatibility if model changed or just use attachment_url
    for img in images:
        img.attachment = img.attachment_url # Shim for template
        
    return render_template('receipts.html', images=images, cats=CATS)

@bp.route('/api/receipt/analyze', methods=['POST'])
def analyze_receipt():
    if 'user_id' not in session: return {"error": "Unauthorized"}, 401
    
    file = request.files.get('file')
    if not file: return {"error": "No file uploaded"}, 400
    
    filename = secure_filename(file.filename)
    fpath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
    file.save(fpath)
    
    api_key = os.getenv('GOOGLE_API_KEY')
    if not api_key: return {"error": "AI Config Missing"}, 500

    try:
        genai.configure(api_key=api_key)
        
        # This model name appeared in your list_models() output
        model_name = 'gemini-flash-latest'
        model = genai.GenerativeModel(model_name)
        
        with open(fpath, "rb") as f:
            image_data = f.read()
            
        mime_type = "image/jpeg"
        if filename.lower().endswith('.pdf'): mime_type = "application/pdf"
        elif filename.lower().endswith('.png'): mime_type = "image/png"

        prompt = """
        You are a receipt analysis engine.
        Extract structured financial data from this receipt.
        Return ONLY valid JSON.

        {
          "merchant": "string",
          "total_amount": number,
          "currency": "string",
          "date": "YYYY-MM-DD",
          "category": "Food/Travel/Shopping/Bills/Health/others",
          "confidence_score": number
        }
        No extra text allowed.
        """
        
        try:
            response = model.generate_content([prompt, {'mime_type': mime_type, 'data': image_data}])
            content = response.text.strip()
            if content.startswith('```json'): content = content[7:-3]
            if content.endswith('```'): content = content[:-3]
            
            data = json.loads(content)
            return {
                "success": True,
                "data": data,
                "filename": filename
            }
        except genai.types.BlockedPromptException as e:
            with open("ai_error_log.txt", "a") as log:
                log.write(f"[{datetime.utcnow()}] AI BLOCKED PROMPT ERROR: {str(e)}\n")
            return {"success": False, "error": "AI blocked the prompt due to safety concerns."}, 400
        except Exception as e:
            with open("ai_error_log.txt", "a") as log:
                log.write(f"[{datetime.utcnow()}] AI EXTRACTION ERROR: {str(e)}\n")
            # Check for rate limit error (429) specifically
            if "429" in str(e):
                return {"success": False, "error": "AI service is busy. Please try again later."}, 429
            return {"success": False, "error": "AI processing failed. Please try again later."}, 500
    except Exception as e:
        with open("ai_error_log.txt", "a") as f:
            f.write(f"[{datetime.utcnow()}] AI ERROR: {str(e)}\n")
        print(f"AI ERROR: {e}")
        return {"success": False, "error": str(e)}, 500

@bp.route('/manual')
def manual():
    if 'user_id' not in session: return redirect(url_for('auth.login'))
    u_id = session['user_id']
    expenses = Expense.query.filter_by(user_id=u_id, is_parsed=False, attachment_url=None).order_by(Expense.expense_date.desc()).all()
    m_paid = db.session.query(func.sum(Expense.amount)).filter_by(user_id=u_id, is_parsed=False, type='Paid').scalar() or 0
    m_received = db.session.query(func.sum(Expense.amount)).filter_by(user_id=u_id, is_parsed=False, type='Received').scalar() or 0
    cat_sum = db.session.query(Expense.category, func.sum(Expense.amount)).filter_by(user_id=u_id, is_parsed=False, type='Paid').group_by(Expense.category).all()
    
    daily_labels, daily_values = [], []
    for i in range(6, -1, -1):
        d = (datetime.utcnow() - timedelta(days=i)).date()
        amt = db.session.query(func.sum(Expense.amount)).filter_by(user_id=u_id, is_parsed=False, type='Paid').filter(func.date(Expense.expense_date) == d).scalar() or 0
        daily_labels.append(d.strftime('%b %d')); daily_values.append(amt)
    return render_template('manual.html', expenses=expenses, cats=CATS, pie_labels=[r[0] for r in cat_sum], pie_values=[r[1] for r in cat_sum], daily_labels=daily_labels, daily_values=daily_values, m_paid=m_paid, m_received=m_received, sel_cat='All')

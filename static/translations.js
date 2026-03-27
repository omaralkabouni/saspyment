
const TRANSLATIONS = {
    en: {
        // Navbar
        'dashboard': 'Dashboard',
        'payments': 'Payments',
        'expenses': 'Expenses',
        'complaints': 'Complaints',
        'report': 'Report',
        'admins': 'Admins',
        'webhook': 'Webhook',
        'backup': 'BACKUP',
        'pass': 'PASSWORD',
        'logout': 'LOGOUT',
        // Dashboard
        'total_users': 'Total Users',
        'active_users': 'Active Users',
        'expired_users': 'Expired Users',
        'online_users': 'Online Users',
        'search_placeholder': 'Search by name, username, phone...',
        'area': 'Area',
        'all_areas': 'All Areas',
        'status': 'Status',
        'all_statuses': 'All',
        'active': 'Active',
        'expired': 'Expired',
        'suspended': 'Suspended',
        // Complaints page
        'new_complaint': 'Register New Complaint',
        'search_subscriber': 'Search for subscriber',
        'complaint_details': 'Complaint details',
        'save_complaint': 'Save Complaint',
        'connection_type': 'Connection Type',
        'dish': 'Dish',
        'box': 'Box',
        'dish_ip': 'Dish IP',
        'not_determined': 'Not determined',
        'total_complaints': 'Total Complaints',
        'my_tasks': 'My Tasks',
        'resolved_by_me': 'Resolved by Me',
        'all_resolved': 'All Resolved',
        'all_agents': 'All Agents',
        'unassigned': 'Unassigned',
        'search': 'Search',
        'all': 'All',
        'my_tasks_only': 'My Tasks Only',
        'new': 'New',
        'resolved': 'Resolved',
        'update_status': 'Update Status',
        'subscriber': 'Subscriber',
        'assigned_to': 'Assigned To',
        'open': 'Open',
        'in_progress': 'In Progress',
        'closed': 'Closed',
        'not_assigned': 'Not Assigned',
    },
    ar: {
        // Navbar
        'dashboard': 'لوحة التحكم',
        'payments': 'الدفعات',
        'expenses': 'المصروفات',
        'complaints': 'الشكاوى',
        'report': 'التقارير',
        'admins': 'المشرفون',
        'webhook': 'ويب هوك',
        'backup': 'نسخ احتياطي',
        'pass': 'كلمة المرور',
        'logout': 'تسجيل الخروج',
        // Dashboard
        'total_users': 'إجمالي المستخدمين',
        'active_users': 'المستخدمون النشطون',
        'expired_users': 'المنتهية صلاحيتهم',
        'online_users': 'المتصلون الآن',
        'search_placeholder': 'بحث بالاسم، المستخدم، الهاتف...',
        'area': 'المنطقة',
        'all_areas': 'جميع المناطق',
        'status': 'الحالة',
        'all_statuses': 'الكل',
        'active': 'نشط',
        'expired': 'منتهي',
        'suspended': 'معلق',
        // Complaints
        'new_complaint': 'تسجيل شكوى جديدة',
        'search_subscriber': 'بحث عن المشترك',
        'complaint_details': 'تفاصيل الشكوى',
        'save_complaint': 'حفظ الشكوى',
        'connection_type': 'نوع الإتصال',
        'dish': 'صحن',
        'box': 'علبة',
        'dish_ip': 'أيبي الصحن',
        'not_determined': 'غير محدد',
        'total_complaints': 'إجمالي الشكاوي',
        'my_tasks': 'مهامي الحالية',
        'resolved_by_me': 'حللتها أنا',
        'all_resolved': 'انتهت بالكامل',
        'all_agents': 'جميع المكلفين',
        'unassigned': 'غير مكلف',
        'search': 'بحث',
        'all': 'الكل',
        'my_tasks_only': 'مهامي فقط',
        'new': 'جديد',
        'resolved': 'محلول',
        'update_status': 'تحديث الحالة',
        'subscriber': 'المشترك',
        'assigned_to': 'المكلف به',
        'open': 'مفتوح',
        'in_progress': 'قيد التنفيذ',
        'closed': 'مغلق',
        'not_assigned': 'غير مكلف',
    }
};

function t(key) {
    const lang = document.documentElement.lang || 'ar';
    return (TRANSLATIONS[lang] && TRANSLATIONS[lang][key]) || key;
}

function applyTranslations() {
    const lang = document.documentElement.lang || 'ar';
    // Apply to all elements with data-i18n attribute
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        const translation = (TRANSLATIONS[lang] && TRANSLATIONS[lang][key]);
        if (translation) {
            if (el.tagName === 'INPUT' && (el.type === 'text' || el.type === 'search')) {
                el.placeholder = translation;
            } else {
                el.textContent = translation;
            }
        }
    });
    // Apply dir
    document.documentElement.dir = lang === 'ar' ? 'rtl' : 'ltr';
}

document.addEventListener('DOMContentLoaded', applyTranslations);
